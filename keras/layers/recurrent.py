# -*- coding: utf-8 -*-
from __future__ import absolute_import
import numpy as np
np.set_printoptions(threshold=np.inf)

from .. import backend as K
from .. import activations, initializations, regularizers
from ..engine import Layer, InputSpec

def time_distributed_dense(x, w, b=None, dropout=None,
                           input_dim=None, output_dim=None, timesteps=None):
    '''Apply y.w + b for every temporal slice y of x.
    '''
    if not input_dim:
        input_dim = K.shape(x)[2]
    if not timesteps:
        timesteps = K.shape(x)[1]
    if not output_dim:
        output_dim = K.shape(w)[1]

    if dropout is not None and 0. < dropout < 1.:
        # apply the same dropout pattern at every timestep
        ones = K.ones_like(K.reshape(x[:, 0, :], (-1, input_dim)))
        dropout_matrix = K.dropout(ones, dropout)
        expanded_dropout_matrix = K.repeat(dropout_matrix, timesteps)
        x = K.in_train_phase(x * expanded_dropout_matrix, x)

    # collapse time dimension and batch dimension together
    x = K.reshape(x, (-1, input_dim))
    x = K.dot(x, w)
    if b:
        x = x + b
    # reshape to 3D tensor
    if K.backend() == 'tensorflow':
        x = K.reshape(x, K.stack([-1, timesteps, output_dim]))
        x.set_shape([None, None, output_dim])
    else:
        x = K.reshape(x, (-1, timesteps, output_dim))
    return x


class Recurrent(Layer):
    '''Abstract base class for recurrent layers.
    Do not use in a model -- it's not a valid layer!
    Use its children classes `LSTM`, `GRU` and `SimpleRNN` instead.

    All recurrent layers (`LSTM`, `GRU`, `SimpleRNN`) also
    follow the specifications of this class and accept
    the keyword arguments listed below.

    # Example

    ```python
        # as the first layer in a Sequential model
        model = Sequential()
        model.add(LSTM(32, input_shape=(10, 64)))
        # now model.output_shape == (None, 32)
        # note: `None` is the batch dimension.

        # the following is identical:
        model = Sequential()
        model.add(LSTM(32, input_dim=64, input_length=10))

        # for subsequent layers, not need to specify the input size:
        model.add(LSTM(16))
    ```

    # Arguments
        weights: list of Numpy arrays to set as initial weights.
            The list should have 3 elements, of shapes:
            `[(input_dim, output_dim), (output_dim, output_dim), (output_dim,)]`.
        return_sequences: Boolean. Whether to return the last output
            in the output sequence, or the full sequence.
        go_backwards: Boolean (default False).
            If True, process the input sequence backwards.
        stateful: Boolean (default False). If True, the last state
            for each sample at index i in a batch will be used as initial
            state for the sample of index i in the following batch.
        unroll: Boolean (default False). If True, the network will be unrolled,
            else a symbolic loop will be used. When using TensorFlow, the network
            is always unrolled, so this argument does not do anything.
            Unrolling can speed-up a RNN, although it tends to be more memory-intensive.
            Unrolling is only suitable for short sequences.
        consume_less: one of "cpu", "mem", or "gpu" (LSTM/GRU only).
            If set to "cpu", the RNN will use
            an implementation that uses fewer, larger matrix products,
            thus running faster on CPU but consuming more memory.
            If set to "mem", the RNN will use more matrix products,
            but smaller ones, thus running slower (may actually be faster on GPU)
            while consuming less memory.
            If set to "gpu" (LSTM/GRU only), the RNN will combine the input gate,
            the forget gate and the output gate into a single matrix,
            enabling more time-efficient parallelization on the GPU. Note: RNN
            dropout must be shared for all gates, resulting in a slightly
            reduced regularization.
        input_dim: dimensionality of the input (integer).
            This argument (or alternatively, the keyword argument `input_shape`)
            is required when using this layer as the first layer in a model.
        input_length: Length of input sequences, to be specified
            when it is constant.
            This argument is required if you are going to connect
            `Flatten` then `Dense` layers upstream
            (without it, the shape of the dense outputs cannot be computed).
            Note that if the recurrent layer is not the first layer
            in your model, you would need to specify the input length
            at the level of the first layer
            (e.g. via the `input_shape` argument)

    # Input shape
        3D tensor with shape `(nb_samples, timesteps, input_dim)`.

    # Output shape
        - if `return_sequences`: 3D tensor with shape
            `(nb_samples, timesteps, output_dim)`.
        - else, 2D tensor with shape `(nb_samples, output_dim)`.

    # Masking
        This layer supports masking for input data with a variable number
        of timesteps. To introduce masks to your data,
        use an [Embedding](embeddings.md) layer with the `mask_zero` parameter
        set to `True`.

    # Note on performance
        You are likely to see better performance with RNNs in Theano compared
        to TensorFlow. Additionally, when using TensorFlow, it is often
        preferable to set `unroll=True` for better performance.

    # Note on using statefulness in RNNs
        You can set RNN layers to be 'stateful', which means that the states
        computed for the samples in one batch will be reused as initial states
        for the samples in the next batch.
        This assumes a one-to-one mapping between
        samples in different successive batches.

        To enable statefulness:
            - specify `stateful=True` in the layer constructor.
            - specify a fixed batch size for your model, by passing
                if sequential model:
                  a `batch_input_shape=(...)` to the first layer in your model.
                else for functional model with 1 or more Input layers:
                  a `batch_shape=(...)` to all the first layers in your model.
                This is the expected shape of your inputs *including the batch size*.
                It should be a tuple of integers, e.g. `(32, 10, 100)`.

        To reset the states of your model, call `.reset_states()` on either
        a specific layer, or on your entire model.
    '''
    def __init__(self, weights=None,
                 return_sequences=False, go_backwards=False, stateful=False,
                 unroll=False, consume_less='gpu',
                 input_dim=None, input_length=None, **kwargs):
        self.return_sequences = return_sequences
        self.initial_weights = weights
        self.go_backwards = go_backwards
        self.stateful = stateful
        self.unroll = unroll
        self.consume_less = consume_less

        self.supports_masking = True
        self.input_spec = [InputSpec(ndim=3)]
        self.input_dim = input_dim
        self.input_length = input_length
        if self.input_dim:
            kwargs['input_shape'] = (self.input_length, self.input_dim)
        super(Recurrent, self).__init__(**kwargs)

    def get_output_shape_for(self, input_shape):
        if self.return_sequences:
            return input_shape[0], input_shape[1], self.output_dim
        else:
            return input_shape[0], self.output_dim

    def compute_mask(self, input, mask):
        if self.return_sequences:
            return mask
        else:
            return None

    def step(self, x, states):
        raise NotImplementedError

    def get_constants(self, x):
        return []

    def get_initial_states(self, x):
        # build an all-zero tensor of shape (samples, output_dim)
        initial_state = K.zeros_like(x)  # (samples, timesteps, input_dim)
        initial_state = K.sum(initial_state, axis=(1, 2))  # (samples,)
        initial_state = K.expand_dims(initial_state)  # (samples, 1)
        initial_state = K.tile(initial_state, [1, self.output_dim])  # (samples, output_dim)
        initial_states = [initial_state for _ in range(len(self.states))]
        return initial_states

    def preprocess_input(self, x):
        return x

    def call(self, x, mask=None):
        # input shape: (nb_samples, time (padded with zeros), input_dim)
        # note that the .build() method of subclasses MUST define
        # self.input_spec with a complete input shape.
        input_shape = K.int_shape(x)
        if self.unroll and input_shape[1] is None:
            raise ValueError('Cannot unroll a RNN if the '
                             'time dimension is undefined. \n'
                             '- If using a Sequential model, '
                             'specify the time dimension by passing '
                             'an `input_shape` or `batch_input_shape` '
                             'argument to your first layer. If your '
                             'first layer is an Embedding, you can '
                             'also use the `input_length` argument.\n'
                             '- If using the functional API, specify '
                             'the time dimension by passing a `shape` '
                             'or `batch_shape` argument to your Input layer.')
        if self.stateful:
            initial_states = self.states
        else:
            initial_states = self.get_initial_states(x)
        constants = self.get_constants(x)
        preprocessed_input = self.preprocess_input(x)
        last_output, outputs, states = K.rnn(self.step, preprocessed_input,
                                             initial_states,
                                             go_backwards=self.go_backwards,
                                             mask=mask,
                                             constants=constants,
                                             unroll=self.unroll,
                                             input_length=input_shape[1])
        if self.stateful:
            updates = []
            for i in range(len(states)):
                updates.append((self.states[i], states[i]))
            self.add_update(updates, x)

        if self.return_sequences:
            return outputs
        else:
            return last_output

    def get_config(self):
        config = {'return_sequences': self.return_sequences,
                  'go_backwards': self.go_backwards,
                  'stateful': self.stateful,
                  'unroll': self.unroll,
                  'consume_less': self.consume_less}
        if self.stateful and self.input_spec[0].shape:
            config['batch_input_shape'] = self.input_spec[0].shape
        else:
            config['input_dim'] = self.input_dim
            config['input_length'] = self.input_length

        base_config = super(Recurrent, self).get_config()
        return dict(list(base_config.items()) + list(config.items()))


class SimpleRNN(Recurrent):
    '''Fully-connected RNN where the output is to be fed back to input.

    # Arguments
        output_dim: dimension of the internal projections and the final output.
        init: weight initialization function.
            Can be the name of an existing function (str),
            or a Theano function (see: [initializations](../initializations.md)).
        inner_init: initialization function of the inner cells.
        activation: activation function.
            Can be the name of an existing function (str),
            or a Theano function (see: [activations](../activations.md)).
        W_regularizer: instance of [WeightRegularizer](../regularizers.md)
            (eg. L1 or L2 regularization), applied to the input weights matrices.
        U_regularizer: instance of [WeightRegularizer](../regularizers.md)
            (eg. L1 or L2 regularization), applied to the recurrent weights matrices.
        b_regularizer: instance of [WeightRegularizer](../regularizers.md),
            applied to the bias.
        dropout_W: float between 0 and 1. Fraction of the input units to drop for input gates.
        dropout_U: float between 0 and 1. Fraction of the input units to drop for recurrent connections.

    # References
        - [A Theoretically Grounded Application of Dropout in Recurrent Neural Networks](http://arxiv.org/abs/1512.05287)
    '''
    def __init__(self, output_dim,
                 init='glorot_uniform', inner_init='orthogonal',
                 activation='tanh',
                 W_regularizer=None, U_regularizer=None, b_regularizer=None,
                 dropout_W=0., dropout_U=0., **kwargs):
        self.output_dim = output_dim
        self.init = initializations.get(init)
        self.inner_init = initializations.get(inner_init)
        self.activation = activations.get(activation)
        self.W_regularizer = regularizers.get(W_regularizer)
        self.U_regularizer = regularizers.get(U_regularizer)
        self.b_regularizer = regularizers.get(b_regularizer)
        self.dropout_W = dropout_W
        self.dropout_U = dropout_U

        if self.dropout_W or self.dropout_U:
            self.uses_learning_phase = True
        super(SimpleRNN, self).__init__(**kwargs)

    def build(self, input_shape):
        self.input_spec = [InputSpec(shape=input_shape)]
        if self.stateful:
            self.reset_states()
        else:
            # initial states: all-zero tensor of shape (output_dim)
            self.states = [None]
        input_dim = input_shape[2]
        self.input_dim = input_dim

        self.W = self.add_weight((input_dim, self.output_dim),
                                 initializer=self.init,
                                 name='{}_W'.format(self.name),
                                 regularizer=self.W_regularizer)
        self.U = self.add_weight((self.output_dim, self.output_dim),
                                 initializer=self.inner_init,
                                 name='{}_U'.format(self.name),
                                 regularizer=self.U_regularizer)
        self.b = self.add_weight((self.output_dim,),
                                 initializer='zero',
                                 name='{}_b'.format(self.name),
                                 regularizer=self.b_regularizer)

        if self.initial_weights is not None:
            self.set_weights(self.initial_weights)
            del self.initial_weights
        self.built = True

    def reset_states(self):
        assert self.stateful, 'Layer must be stateful.'
        input_shape = self.input_spec[0].shape
        if not input_shape[0]:
            raise ValueError('If a RNN is stateful, it needs to know '
                             'its batch size. Specify the batch size '
                             'of your input tensors: \n'
                             '- If using a Sequential model, '
                             'specify the batch size by passing '
                             'a `batch_input_shape` '
                             'argument to your first layer.\n'
                             '- If using the functional API, specify '
                             'the time dimension by passing a '
                             '`batch_shape` argument to your Input layer.')
        if hasattr(self, 'states'):
            K.set_value(self.states[0],
                        np.zeros((input_shape[0], self.output_dim)))
        else:
            self.states = [K.zeros((input_shape[0], self.output_dim))]

    def preprocess_input(self, x):
        if self.consume_less == 'cpu':
            input_shape = K.int_shape(x)
            input_dim = input_shape[2]
            timesteps = input_shape[1]
            return time_distributed_dense(x, self.W, self.b, self.dropout_W,
                                          input_dim, self.output_dim,
                                          timesteps)
        else:
            return x

    def step(self, x, states):
        prev_output = states[0]
        B_U = states[1]
        B_W = states[2]

        if self.consume_less == 'cpu':
            h = x
        else:
            h = K.dot(x * B_W, self.W) + self.b

        output = self.activation(h + K.dot(prev_output * B_U, self.U))
        return output, [output]

    def get_constants(self, x):
        constants = []
        if 0 < self.dropout_U < 1:
            ones = K.ones_like(K.reshape(x[:, 0, 0], (-1, 1)))
            ones = K.tile(ones, (1, self.output_dim))
            B_U = K.in_train_phase(K.dropout(ones, self.dropout_U), ones)
            constants.append(B_U)
        else:
            constants.append(K.cast_to_floatx(1.))
        if self.consume_less == 'cpu' and 0 < self.dropout_W < 1:
            input_shape = K.int_shape(x)
            input_dim = input_shape[-1]
            ones = K.ones_like(K.reshape(x[:, 0, 0], (-1, 1)))
            ones = K.tile(ones, (1, int(input_dim)))
            B_W = K.in_train_phase(K.dropout(ones, self.dropout_W), ones)
            constants.append(B_W)
        else:
            constants.append(K.cast_to_floatx(1.))
        return constants

    def get_config(self):
        config = {'output_dim': self.output_dim,
                  'init': self.init.__name__,
                  'inner_init': self.inner_init.__name__,
                  'activation': self.activation.__name__,
                  'W_regularizer': self.W_regularizer.get_config() if self.W_regularizer else None,
                  'U_regularizer': self.U_regularizer.get_config() if self.U_regularizer else None,
                  'b_regularizer': self.b_regularizer.get_config() if self.b_regularizer else None,
                  'dropout_W': self.dropout_W,
                  'dropout_U': self.dropout_U}
        base_config = super(SimpleRNN, self).get_config()
        return dict(list(base_config.items()) + list(config.items()))


class GRU(Recurrent):
    '''Gated Recurrent Unit - Cho et al. 2014.

    # Arguments
        output_dim: dimension of the internal projections and the final output.
        init: weight initialization function.
            Can be the name of an existing function (str),
            or a Theano function (see: [initializations](../initializations.md)).
        inner_init: initialization function of the inner cells.
        activation: activation function.
            Can be the name of an existing function (str),
            or a Theano function (see: [activations](../activations.md)).
        inner_activation: activation function for the inner cells.
        W_regularizer: instance of [WeightRegularizer](../regularizers.md)
            (eg. L1 or L2 regularization), applied to the input weights matrices.
        U_regularizer: instance of [WeightRegularizer](../regularizers.md)
            (eg. L1 or L2 regularization), applied to the recurrent weights matrices.
        b_regularizer: instance of [WeightRegularizer](../regularizers.md),
            applied to the bias.
        dropout_W: float between 0 and 1. Fraction of the input units to drop for input gates.
        dropout_U: float between 0 and 1. Fraction of the input units to drop for recurrent connections.

    # References
        - [On the Properties of Neural Machine Translation: Encoder-Decoder Approaches](http://www.aclweb.org/anthology/W14-4012)
        - [Empirical Evaluation of Gated Recurrent Neural Networks on Sequence Modeling](http://arxiv.org/pdf/1412.3555v1.pdf)
        - [A Theoretically Grounded Application of Dropout in Recurrent Neural Networks](http://arxiv.org/abs/1512.05287)
    '''
    def __init__(self, output_dim,
                 init='glorot_uniform', inner_init='orthogonal',
                 activation='tanh', inner_activation='hard_sigmoid',
                 W_regularizer=None, U_regularizer=None, b_regularizer=None,
                 dropout_W=0., dropout_U=0., **kwargs):
        self.output_dim = output_dim
        self.init = initializations.get(init)
        self.inner_init = initializations.get(inner_init)
        self.activation = activations.get(activation)
        self.inner_activation = activations.get(inner_activation)
        self.W_regularizer = regularizers.get(W_regularizer)
        self.U_regularizer = regularizers.get(U_regularizer)
        self.b_regularizer = regularizers.get(b_regularizer)
        self.dropout_W = dropout_W
        self.dropout_U = dropout_U

        if self.dropout_W or self.dropout_U:
            self.uses_learning_phase = True
        super(GRU, self).__init__(**kwargs)

    def build(self, input_shape):
        self.input_spec = [InputSpec(shape=input_shape)]
        self.input_dim = input_shape[2]

        if self.stateful:
            self.reset_states()
        else:
            # initial states: all-zero tensor of shape (output_dim)
            self.states = [None]

        if self.consume_less == 'gpu':
            self.W = self.add_weight((self.input_dim, 3 * self.output_dim),
                                     initializer=self.init,
                                     name='{}_W'.format(self.name),
                                     regularizer=self.W_regularizer)
            self.U = self.add_weight((self.output_dim, 3 * self.output_dim),
                                     initializer=self.inner_init,
                                     name='{}_U'.format(self.name),
                                     regularizer=self.U_regularizer)
            self.b = self.add_weight((self.output_dim * 3,),
                                     initializer='zero',
                                     name='{}_b'.format(self.name),
                                     regularizer=self.b_regularizer)
        else:
            self.W_z = self.add_weight((self.input_dim, self.output_dim),
                                       initializer=self.init,
                                       name='{}_W_z'.format(self.name),
                                       regularizer=self.W_regularizer)
            self.U_z = self.add_weight((self.output_dim, self.output_dim),
                                       initializer=self.init,
                                       name='{}_U_z'.format(self.name),
                                       regularizer=self.W_regularizer)
            self.b_z = self.add_weight((self.output_dim,),
                                       initializer='zero',
                                       name='{}_b_z'.format(self.name),
                                       regularizer=self.b_regularizer)
            self.W_r = self.add_weight((self.input_dim, self.output_dim),
                                       initializer=self.init,
                                       name='{}_W_r'.format(self.name),
                                       regularizer=self.W_regularizer)
            self.U_r = self.add_weight((self.output_dim, self.output_dim),
                                       initializer=self.init,
                                       name='{}_U_r'.format(self.name),
                                       regularizer=self.W_regularizer)
            self.b_r = self.add_weight((self.output_dim,),
                                       initializer='zero',
                                       name='{}_b_r'.format(self.name),
                                       regularizer=self.b_regularizer)
            self.W_h = self.add_weight((self.input_dim, self.output_dim),
                                       initializer=self.init,
                                       name='{}_W_h'.format(self.name),
                                       regularizer=self.W_regularizer)
            self.U_h = self.add_weight((self.output_dim, self.output_dim),
                                       initializer=self.init,
                                       name='{}_U_h'.format(self.name),
                                       regularizer=self.W_regularizer)
            self.b_h = self.add_weight((self.output_dim,),
                                       initializer='zero',
                                       name='{}_b_h'.format(self.name),
                                       regularizer=self.b_regularizer)
            self.W = K.concatenate([self.W_z, self.W_r, self.W_h])
            self.U = K.concatenate([self.U_z, self.U_r, self.U_h])
            self.b = K.concatenate([self.b_z, self.b_r, self.b_h])

        if self.initial_weights is not None:
            self.set_weights(self.initial_weights)
            del self.initial_weights
        self.built = True

    def reset_states(self):
        assert self.stateful, 'Layer must be stateful.'
        input_shape = self.input_spec[0].shape
        if not input_shape[0]:
            raise ValueError('If a RNN is stateful, a complete ' +
                             'input_shape must be provided (including batch size).')
        if hasattr(self, 'states'):
            K.set_value(self.states[0],
                        np.zeros((input_shape[0], self.output_dim)))
        else:
            self.states = [K.zeros((input_shape[0], self.output_dim))]

    def preprocess_input(self, x):
        if self.consume_less == 'cpu':
            input_shape = K.int_shape(x)
            input_dim = input_shape[2]
            timesteps = input_shape[1]

            x_z = time_distributed_dense(x, self.W_z, self.b_z, self.dropout_W,
                                         input_dim, self.output_dim, timesteps)
            x_r = time_distributed_dense(x, self.W_r, self.b_r, self.dropout_W,
                                         input_dim, self.output_dim, timesteps)
            x_h = time_distributed_dense(x, self.W_h, self.b_h, self.dropout_W,
                                         input_dim, self.output_dim, timesteps)
            return K.concatenate([x_z, x_r, x_h], axis=2)
        else:
            return x

    def step(self, x, states):
        h_tm1 = states[0]  # previous memory
        B_U = states[1]  # dropout matrices for recurrent units
        B_W = states[2]

        if self.consume_less == 'gpu':

            matrix_x = K.dot(x * B_W[0], self.W) + self.b
            matrix_inner = K.dot(h_tm1 * B_U[0], self.U[:, :2 * self.output_dim])

            x_z = matrix_x[:, :self.output_dim]
            x_r = matrix_x[:, self.output_dim: 2 * self.output_dim]
            inner_z = matrix_inner[:, :self.output_dim]
            inner_r = matrix_inner[:, self.output_dim: 2 * self.output_dim]

            z = self.inner_activation(x_z + inner_z)
            r = self.inner_activation(x_r + inner_r)

            x_h = matrix_x[:, 2 * self.output_dim:]
            inner_h = K.dot(r * h_tm1 * B_U[0], self.U[:, 2 * self.output_dim:])
            hh = self.activation(x_h + inner_h)
        else:
            if self.consume_less == 'cpu':
                x_z = x[:, :self.output_dim]
                x_r = x[:, self.output_dim: 2 * self.output_dim]
                x_h = x[:, 2 * self.output_dim:]
            elif self.consume_less == 'mem':
                x_z = K.dot(x * B_W[0], self.W_z) + self.b_z
                x_r = K.dot(x * B_W[1], self.W_r) + self.b_r
                x_h = K.dot(x * B_W[2], self.W_h) + self.b_h
            else:
                raise ValueError('Unknown `consume_less` mode.')
            z = self.inner_activation(x_z + K.dot(h_tm1 * B_U[0], self.U_z))
            r = self.inner_activation(x_r + K.dot(h_tm1 * B_U[1], self.U_r))

            hh = self.activation(x_h + K.dot(r * h_tm1 * B_U[2], self.U_h))
        h = z * h_tm1 + (1 - z) * hh
        return h, [h]

    def get_constants(self, x):
        constants = []
        if 0 < self.dropout_U < 1:
            ones = K.ones_like(K.reshape(x[:, 0, 0], (-1, 1)))
            ones = K.tile(ones, (1, self.output_dim))
            B_U = [K.in_train_phase(K.dropout(ones, self.dropout_U), ones) for _ in range(3)]
            constants.append(B_U)
        else:
            constants.append([K.cast_to_floatx(1.) for _ in range(3)])

        if 0 < self.dropout_W < 1:
            input_shape = K.int_shape(x)
            input_dim = input_shape[-1]
            ones = K.ones_like(K.reshape(x[:, 0, 0], (-1, 1)))
            ones = K.tile(ones, (1, int(input_dim)))
            B_W = [K.in_train_phase(K.dropout(ones, self.dropout_W), ones) for _ in range(3)]
            constants.append(B_W)
        else:
            constants.append([K.cast_to_floatx(1.) for _ in range(3)])
        return constants

    def get_config(self):
        config = {'output_dim': self.output_dim,
                  'init': self.init.__name__,
                  'inner_init': self.inner_init.__name__,
                  'activation': self.activation.__name__,
                  'inner_activation': self.inner_activation.__name__,
                  'W_regularizer': self.W_regularizer.get_config() if self.W_regularizer else None,
                  'U_regularizer': self.U_regularizer.get_config() if self.U_regularizer else None,
                  'b_regularizer': self.b_regularizer.get_config() if self.b_regularizer else None,
                  'dropout_W': self.dropout_W,
                  'dropout_U': self.dropout_U}
        base_config = super(GRU, self).get_config()
        return dict(list(base_config.items()) + list(config.items()))


class GRUCond(Recurrent):
    '''Gated Recurrent Unit - Cho et al. 2014. with the previously generated word fed to the current timestep.
    You should give two inputs to this layer:
        1. The shifted sequence of words (shape: (mini_batch_size, output_timesteps, embedding_size))

    # Arguments
        output_dim: dimension of the internal projections and the final output.
        init: weight initialization function.
            Can be the name of an existing function (str),
            or a Theano function (see: [initializations](../initializations.md)).
        inner_init: initialization function of the inner cells.
        return_states: boolean indicating if we want the intermediate states (hidden_state and memory) as additional outputs
        activation: activation function.
            Can be the name of an existing function (str),
            or a Theano function (see: [activations](../activations.md)).
        inner_activation: activation function for the inner cells.
        W_regularizer: instance of [WeightRegularizer](../regularizers.md)
            (eg. L1 or L2 regularization), applied to the input weights matrices.
        U_regularizer: instance of [WeightRegularizer](../regularizers.md)
            (eg. L1 or L2 regularization), applied to the recurrent weights matrices.
        b_regularizer: instance of [WeightRegularizer](../regularizers.md),
            applied to the bias.
        dropout_W: float between 0 and 1. Fraction of the input units to drop for input gates.
        dropout_U: float between 0 and 1. Fraction of the input units to drop for recurrent connections.
        w_a_regularizer: instance of [WeightRegularizer](../regularizers.md)
            (eg. L1 or L2 regularization), applied to the input weights matrices.
        W_a_regularizer: instance of [WeightRegularizer](../regularizers.md)
            (eg. L1 or L2 regularization), applied to the input weights matrices.
        U_a_regularizer: instance of [WeightRegularizer](../regularizers.md)
            (eg. L1 or L2 regularization), applied to the recurrent weights matrices.
        b_a_regularizer: instance of [WeightRegularizer](../regularizers.md),
            applied to the bias.
        dropout_w_a: float between 0 and 1.
        dropout_W_a: float between 0 and 1.
        dropout_U_a: float between 0 and 1.


    # Formulation

        The resulting attention vector 'phi' at time 't' is formed by applying a weighted sum over
        the set of inputs 'x_i' contained in 'X':

            phi(X, t) = ∑_i alpha_i(t) * x_i,

        where each 'alpha_i' at time 't' is a weighting vector over all the input dimension that
        accomplishes the following condition:

            ∑_i alpha_i = 1

        and is dynamically adapted at each timestep w.r.t. the following formula:

            alpha_i(t) = exp{e_i(t)} /  ∑_j exp{e_j(t)}

        where each 'e_i' at time 't' is calculated as:

            e_i(t) = wa' * tanh( Wa * x_i  +  Ua * h(t-1)  +  ba ),

        where the following are learnable with the respectively named sizes:
                wa                Wa                     Ua                 ba
            [input_dim] [input_dim, input_dim] [output_dim, input_dim] [input_dim]

        The names of 'Ua' and 'Wa' are exchanged w.r.t. the provided reference as well as 'v' being renamed
        to 'x' for matching Keras LSTM's nomenclature.


    # References
        - [On the Properties of Neural Machine Translation: Encoder–Decoder Approaches](http://www.aclweb.org/anthology/W14-4012)
        - [Empirical Evaluation of Gated Recurrent Neural Networks on Sequence Modeling:(http://arxiv.org/pdf/1412.3555v1.pdf)
        - [Long short-term memory](http://deeplearning.cs.cmu.edu/pdfs/Hochreiter97_lstm.pdf) (original 1997 paper)
        - [Learning to forget: Continual prediction with LSTM](http://www.mitpressjournals.org/doi/pdf/10.1162/089976600300015015)
        - [Supervised sequence labeling with recurrent neural networks](http://www.cs.toronto.edu/~graves/preprint.pdf)
        - [A Theoretically Grounded Application of Dropout in Recurrent Neural Networks](http://arxiv.org/abs/1512.05287)
    '''
    def __init__(self, output_dim,
                 init='glorot_uniform', inner_init='orthogonal',
                 return_states=False,
                 activation='tanh', inner_activation='hard_sigmoid',
                 W_regularizer=None, U_regularizer=None, V_regularizer=None, b_regularizer=None,
                 dropout_W=0., dropout_U=0., dropout_V=0., **kwargs):
        self.output_dim = output_dim
        self.init = initializations.get(init)
        self.inner_init = initializations.get(inner_init)
        self.return_states = return_states
        self.activation = activations.get(activation)
        self.inner_activation = activations.get(inner_activation)
        self.W_regularizer = regularizers.get(W_regularizer)
        self.U_regularizer = regularizers.get(U_regularizer)
        self.V_regularizer = regularizers.get(V_regularizer)
        self.b_regularizer = regularizers.get(b_regularizer)
        self.dropout_W, self.dropout_U, self.dropout_V  = dropout_W, dropout_U, dropout_V

        if self.dropout_W or self.dropout_U or self.dropout_V:
            self.uses_learning_phase = True
        super(GRUCond, self).__init__(**kwargs)

    def build(self, input_shape):
        assert len(input_shape) == 2 or len(input_shape) == 3, 'You should pass two inputs to LSTMAttnCond ' \
                                                               '(previous_embedded_words and context) and ' \
                                                               'one optional input (init_memory)'

        if len(input_shape) == 2:
            self.input_spec = [InputSpec(shape=input_shape[0]), InputSpec(shape=input_shape[1])]
            self.num_inputs = 2
        elif len(input_shape) == 3:
            self.input_spec = [InputSpec(shape=input_shape[0]),
                               InputSpec(shape=input_shape[1]),
                               InputSpec(shape=input_shape[2])]
            self.num_inputs = 3
        self.input_dim = input_shape[0][2]
        self.context_dim = input_shape[1][1]

        if self.stateful:
            self.reset_states()
        else:
            # initial states: all-zero tensor of shape (output_dim)
            self.states = [None]

        if self.consume_less == 'gpu':
            self.V = self.add_weight((self.input_dim, 3 * self.output_dim),
                                     initializer=self.inner_init,
                                     name='{}_V'.format(self.name),
                                     regularizer=self.V_regularizer)
            self.W = self.add_weight((self.context_dim, 3 * self.output_dim),
                                     initializer=self.init,
                                     name='{}_W'.format(self.name),
                                     regularizer=self.W_regularizer)
            self.U = self.add_weight((self.output_dim, 3 * self.output_dim),
                                     initializer=self.inner_init,
                                     name='{}_U'.format(self.name),
                                     regularizer=self.U_regularizer)
            self.b = self.add_weight((self.output_dim * 3,),
                                     initializer='zero',
                                     name='{}_b'.format(self.name),
                                     regularizer=self.b_regularizer)

            self.trainable_weights =  [self.V, # Cond weights
                                       self.W, self.U, self.b]
        else:
            self.V_z = self.add_weight((self.input_dim, self.output_dim),
                                       initializer=self.inner_init,
                                       name='{}_V_z'.format(self.name),
                                       regularizer=self.W_regularizer)
            self.W_z = self.add_weight((self.context_dim, self.output_dim),
                                       initializer=self.init,
                                       name='{}_W_z'.format(self.name),
                                       regularizer=self.W_regularizer)
            self.U_z = self.add_weight((self.output_dim, self.output_dim),
                                       initializer=self.init,
                                       name='{}_U_z'.format(self.name),
                                       regularizer=self.W_regularizer)
            self.b_z = self.add_weight((self.output_dim,),
                                       initializer='zero',
                                       name='{}_b_z'.format(self.name),
                                       regularizer=self.b_regularizer)
            self.V_r = self.add_weight((self.input_dim, self.output_dim),
                                       initializer=self.inner_init,
                                       name='{}_V_r'.format(self.name),
                                       regularizer=self.W_regularizer)
            self.W_r = self.add_weight((self.context_dim, self.output_dim),
                                       initializer=self.init,
                                       name='{}_W_r'.format(self.name),
                                       regularizer=self.W_regularizer)
            self.U_r = self.add_weight((self.output_dim, self.output_dim),
                                       initializer=self.init,
                                       name='{}_U_r'.format(self.name),
                                       regularizer=self.W_regularizer)
            self.b_r = self.add_weight((self.output_dim,),
                                       initializer='zero',
                                       name='{}_b_r'.format(self.name),
                                       regularizer=self.b_regularizer)
            self.V_h = self.add_weight((self.input_dim, self.output_dim),
                                       initializer=self.inner_init,
                                       name='{}_V_h'.format(self.name),
                                       regularizer=self.W_regularizer)
            self.W_h = self.add_weight((self.context_dim, self.output_dim),
                                       initializer=self.init,
                                       name='{}_W_h'.format(self.name),
                                       regularizer=self.W_regularizer)
            self.U_h = self.add_weight((self.output_dim, self.output_dim),
                                       initializer=self.init,
                                       name='{}_U_h'.format(self.name),
                                       regularizer=self.W_regularizer)
            self.b_h = self.add_weight((self.output_dim,),
                                       initializer='zero',
                                       name='{}_b_h'.format(self.name),
                                       regularizer=self.b_regularizer)


            self.trainable_weights = [self.V_z, self.W_r, self.U_h, self.b_z,
                                      self.V_r, self.W_r, self.U_r, self.b_r,
                                      self.V_h, self.W_h, self.U_h, self.b_h
                                      ]


            self.W = K.concatenate([self.W_z, self.W_r, self.W_h])
            self.U = K.concatenate([self.U_z, self.U_r, self.U_h])
            self.b = K.concatenate([self.b_z, self.b_r, self.b_h])

        if self.initial_weights is not None:
            self.set_weights(self.initial_weights)
            del self.initial_weights
        self.built = True

    def reset_states(self):
        assert self.stateful, 'Layer must be stateful.'
        input_shape = self.input_spec[0].shape
        if not input_shape[0]:
            raise ValueError('If a RNN is stateful, a complete ' +
                             'input_shape must be provided (including batch size).')
        if hasattr(self, 'states'):
            K.set_value(self.states[0],
                        np.zeros((input_shape[0], self.output_dim)))
            K.set_value(self.states[1],
                        np.zeros((input_shape[0], self.output_dim)))
            K.set_value(self.states[2],
                        np.zeros((input_shape[0], input_shape[3])))
        else:
            self.states = [K.zeros((input_shape[0], self.output_dim)),
               K.zeros((input_shape[0], self.output_dim)),
               K.zeros((input_shape[0], input_shape[3]))]

    def preprocess_input(self, x, B_V):
        return K.dot(x * B_V[0], self.V) + self.b

    def get_output_shape_for(self, input_shape):
        if self.return_sequences:
            main_out = (input_shape[0][0], input_shape[0][1], self.output_dim)
        else:
            main_out = (input_shape[0][0], self.output_dim)

        if self.return_states:
            states_dim = (input_shape[0][0], input_shape[0][1], self.output_dim)
            main_out = [main_out, states_dim]

        return main_out

    def call(self, x, mask=None):
        # input shape: (nb_samples, time (padded with zeros), input_dim)
        # note that the .build() method of subclasses MUST define
        # self.input_spec with a complete input shape.

        input_shape = self.input_spec[0].shape
        state_below = x[0]
        self.context = x[1]
        if self.num_inputs == 2: # input: [state_below, context]
            self.init_state = None
            self.init_memory = None
        elif self.num_inputs == 3: # input: [state_below, context, init_hidden_state]
            self.init_state = x[2]
            self.init_memory = None

        if K._BACKEND == 'tensorflow':
            if not input_shape[1]:
                raise Exception('When using TensorFlow, you should define '
                                'explicitly the number of timesteps of '
                                'your sequences.\n'
                                'If your first layer is an Embedding, '
                                'make sure to pass it an "input_length" '
                                'argument. Otherwise, make sure '
                                'the first layer has '
                                'an "input_shape" or "batch_input_shape" '
                                'argument, including the time axis. '
                                'Found input shape at layer ' + self.name +
                                ': ' + str(input_shape))
        if self.stateful:
            initial_states = self.states
        else:
            initial_states = self.get_initial_states(state_below)

        constants, B_V = self.get_constants(state_below)
        preprocessed_input = self.preprocess_input(state_below, B_V)
        last_output, outputs, states = K.rnn(self.step, preprocessed_input,
                                             initial_states,
                                             go_backwards=self.go_backwards,
                                             mask=mask[0],
                                             constants=constants,
                                             unroll=self.unroll,
                                             input_length=state_below.shape[1])
        if self.stateful:
            self.updates = []
            for i in range(len(states)):
                self.updates.append((self.states[i], states[i]))

        if self.return_sequences:
            ret = outputs
        else:
            ret = last_output

        # intermediate states as additional outputs
        if self.return_states:
            ret = [ret, states[0]]

        return ret

    def compute_mask(self, input, mask):
        if self.return_sequences:
            ret = mask[0]
        else:
            ret = None
        if self.return_states:
            ret = [ret, None]
        return ret

    def step(self, x, states):
        h_tm1 = states[0]  # previous hidden state

        # dropout matrices for recurrent units
        B_U = states[1]     # Dropout U
        B_W = states[2]     # Dropout W

        # Context (input sequence)
        context = states[3]     # Context

        if self.consume_less == 'gpu':
            matrix_x = x + K.dot(context * B_W[0], self.W)
            matrix_inner = K.dot(h_tm1 * B_U[0], self.U[:, :2 * self.output_dim])

            x_z = matrix_x[:, :self.output_dim]
            x_r = matrix_x[:, self.output_dim: 2 * self.output_dim]
            inner_z = matrix_inner[:, :self.output_dim]
            inner_r = matrix_inner[:, self.output_dim: 2 * self.output_dim]

            z = self.inner_activation(x_z + inner_z)
            r = self.inner_activation(x_r + inner_r)

            x_h = matrix_x[:, 2 * self.output_dim:]
            inner_h = K.dot(r * h_tm1 * B_U[0], self.U[:, 2 * self.output_dim:])
            hh = self.activation(x_h + inner_h)
        h = z * h_tm1 + (1 - z) * hh

        return h, [h]

    def get_constants(self, x):
        constants = []
        # States[1]
        if 0 < self.dropout_U < 1:
            ones = K.ones_like(K.reshape(x[:, 0, 0], (-1, 1)))
            ones = K.concatenate([ones] * self.output_dim, 1)
            B_U = [K.in_train_phase(K.dropout(ones, self.dropout_U), ones) for _ in range(3)]
            constants.append(B_U)
        else:
            constants.append([K.cast_to_floatx(1.) for _ in range(3)])

        # States[2]
        if 0 < self.dropout_W < 1:
            input_shape = self.input_spec[0][0].shape
            input_dim = input_shape[-1]
            ones = K.ones_like(K.reshape(x[:, 0, 0], (-1, 1)))
            ones = K.concatenate([ones] * input_dim, 1)
            B_W = [K.in_train_phase(K.dropout(ones, self.dropout_W), ones) for _ in range(3)]
            constants.append(B_W)
        else:
            constants.append([K.cast_to_floatx(1.) for _ in range(3)])

        if 0 < self.dropout_V < 1:
            input_dim = self.input_dim
            ones = K.ones_like(K.reshape(x[:, :, 0], (-1, x.shape[1], 1))) # (bs, timesteps, 1)
            ones = K.concatenate([ones] * input_dim, axis=2)
            B_V = [K.in_train_phase(K.dropout(ones, self.dropout_V), ones) for _ in range(3)]
        else:
            B_V = [K.cast_to_floatx(1.) for _ in range(3)]

        # States[3]
        constants.append(self.context)

        return constants, B_V

    def get_initial_states(self, x):
        # build an all-zero tensor of shape (samples, output_dim)
        if self.init_state is None:
            #  build an all-zero tensor of shape (samples, output_dim)
            initial_state = K.zeros_like(x)  # (samples, timesteps, input_dim)
            initial_state = K.sum(initial_state, axis=(1, 2))  # (samples,)
            initial_state = K.expand_dims(initial_state)  # (samples, 1)
            initial_state = K.tile(initial_state, [1, self.output_dim])  # (samples, output_dim)
        else:
            initial_state = self.init_state
        initial_states = [initial_state]

        return initial_states

    def get_config(self):
        config = {'output_dim': self.output_dim,
                  'return_states': self.return_states,
                  'init': self.init.__name__,
                  'inner_init': self.inner_init.__name__,
                  'activation': self.activation.__name__,
                  'inner_activation': self.inner_activation.__name__,
                  'W_regularizer': self.W_regularizer.get_config() if self.W_regularizer else None,
                  'U_regularizer': self.U_regularizer.get_config() if self.U_regularizer else None,
                  'V_regularizer': self.V_regularizer.get_config() if self.V_regularizer else None,
                  'b_regularizer': self.b_regularizer.get_config() if self.b_regularizer else None,
                  'dropout_W': self.dropout_W,
                  'dropout_U': self.dropout_U,
                  'dropout_V': self.dropout_V,
                  }
        base_config = super(GRUCond, self).get_config()
        return dict(list(base_config.items()) + list(config.items()))


class AttGRUCond(Recurrent):
    '''Gated Recurrent Unit - Cho et al. 2014. with Attention + the previously generated word fed to the current timestep.
    You should give two inputs to this layer:
        1. The shifted sequence of words (shape: (mini_batch_size, output_timesteps, embedding_size))
        2. The complete input sequence (shape: (mini_batch_size, input_timesteps, input_dim))

    # Arguments
        output_dim: dimension of the internal projections and the final output.
        init: weight initialization function.
            Can be the name of an existing function (str),
            or a Theano function (see: [initializations](../initializations.md)).
        return_extra_variables: indicates if we only need the LSTM hidden state (False) or we want
            additional internal variables as outputs (True). The additional variables provided are:
            - x_att (None, out_timesteps, dim_encoder): feature vector computed after the Att.Model at each timestep
            - alphas (None, out_timesteps, in_timesteps): weights computed by the Att.Model at each timestep
        return_states: boolean indicating if we want the intermediate states (hidden_state and memory) as additional outputs
        inner_init: initialization function of the inner cells.
        activation: activation function.
            Can be the name of an existing function (str),
            or a Theano function (see: [activations](../activations.md)).
        inner_activation: activation function for the inner cells.
        W_regularizer: instance of [WeightRegularizer](../regularizers.md)
            (eg. L1 or L2 regularization), applied to the input weights matrices.
        U_regularizer: instance of [WeightRegularizer](../regularizers.md)
            (eg. L1 or L2 regularization), applied to the recurrent weights matrices.
        b_regularizer: instance of [WeightRegularizer](../regularizers.md),
            applied to the bias.
        dropout_W: float between 0 and 1. Fraction of the input units to drop for input gates.
        dropout_U: float between 0 and 1. Fraction of the input units to drop for recurrent connections.
        w_a_regularizer: instance of [WeightRegularizer](../regularizers.md)
            (eg. L1 or L2 regularization), applied to the input weights matrices.
        W_a_regularizer: instance of [WeightRegularizer](../regularizers.md)
            (eg. L1 or L2 regularization), applied to the input weights matrices.
        U_a_regularizer: instance of [WeightRegularizer](../regularizers.md)
            (eg. L1 or L2 regularization), applied to the recurrent weights matrices.
        b_a_regularizer: instance of [WeightRegularizer](../regularizers.md),
            applied to the bias.
        dropout_w_a: float between 0 and 1.
        dropout_W_a: float between 0 and 1.
        dropout_U_a: float between 0 and 1.


    # Formulation

        The resulting attention vector 'phi' at time 't' is formed by applying a weighted sum over
        the set of inputs 'x_i' contained in 'X':

            phi(X, t) = ∑_i alpha_i(t) * x_i,

        where each 'alpha_i' at time 't' is a weighting vector over all the input dimension that
        accomplishes the following condition:

            ∑_i alpha_i = 1

        and is dynamically adapted at each timestep w.r.t. the following formula:

            alpha_i(t) = exp{e_i(t)} /  ∑_j exp{e_j(t)}

        where each 'e_i' at time 't' is calculated as:

            e_i(t) = wa' * tanh( Wa * x_i  +  Ua * h(t-1)  +  ba ),

        where the following are learnable with the respectively named sizes:
                wa                Wa                     Ua                 ba
            [input_dim] [input_dim, input_dim] [output_dim, input_dim] [input_dim]

        The names of 'Ua' and 'Wa' are exchanged w.r.t. the provided reference as well as 'v' being renamed
        to 'x' for matching Keras LSTM's nomenclature.


    # References
        - [On the Properties of Neural Machine Translation:
            Encoder–Decoder Approaches](http://www.aclweb.org/anthology/W14-4012)
        - [Empirical Evaluation of Gated Recurrent Neural Networks on
            Sequence Modeling](http://arxiv.org/pdf/1412.3555v1.pdf)
        - [A Theoretically Grounded Application of Dropout in
            Recurrent Neural Networks](http://arxiv.org/abs/1512.05287)
    '''
    def __init__(self, output_dim, return_extra_variables=False, return_states=False,
                 init='glorot_uniform', inner_init='orthogonal', init_att='glorot_uniform',
                 activation='tanh', inner_activation='hard_sigmoid', mask_value=0.,
                 W_regularizer=None, U_regularizer=None, V_regularizer=None, b_regularizer=None,
                 wa_regularizer=None, Wa_regularizer=None, Ua_regularizer=None, ba_regularizer=None, ca_regularizer=None,
                 dropout_W=0., dropout_U=0., dropout_V=0., dropout_wa=0., dropout_Wa=0., dropout_Ua=0., **kwargs):
        self.output_dim = output_dim
        self.return_extra_variables = return_extra_variables
        self.return_states = return_states
        self.init = initializations.get(init)
        self.inner_init = initializations.get(inner_init)
        self.init_att = initializations.get(init_att)
        self.activation = activations.get(activation)
        self.inner_activation = activations.get(inner_activation)
        self.W_regularizer =  regularizers.get(W_regularizer)
        self.U_regularizer =  regularizers.get(U_regularizer)
        self.V_regularizer =  regularizers.get(V_regularizer)
        self.b_regularizer =  regularizers.get(b_regularizer)
        # Attention model regularizers
        self.wa_regularizer = regularizers.get(wa_regularizer)
        self.Wa_regularizer = regularizers.get(Wa_regularizer)
        self.Ua_regularizer = regularizers.get(Ua_regularizer)
        self.ba_regularizer = regularizers.get(ba_regularizer)
        self.ca_regularizer = regularizers.get(ca_regularizer)
        self.mask_value = mask_value

        self.dropout_W, self.dropout_U, self.dropout_V  = dropout_W, dropout_U, dropout_V
        self.dropout_wa, self.dropout_Wa, self.dropout_Ua = dropout_wa, dropout_Wa, dropout_Ua

        if self.dropout_W or self.dropout_U or self.dropout_V or self.dropout_wa or self.dropout_Wa or self.dropout_Ua:
            self.uses_learning_phase = True
        super(AttGRUCond, self).__init__(**kwargs)

    def build(self, input_shape):
        assert len(input_shape) == 2 or len(input_shape) == 3, 'You should pass two inputs to LSTMAttnCond ' \
                                                               '(previous_embedded_words and context) and ' \
                                                               'one optional input (init_memory)'

        if len(input_shape) == 2:
            self.input_spec = [InputSpec(shape=input_shape[0]), InputSpec(shape=input_shape[1])]
            self.num_inputs = 2
        elif len(input_shape) == 3:
            self.input_spec = [InputSpec(shape=input_shape[0]),
                               InputSpec(shape=input_shape[1]),
                               InputSpec(shape=input_shape[2])]
            self.num_inputs = 3
        self.input_dim = input_shape[0][2]
        self.context_steps = input_shape[1][1] #if input_shape[0][1] is not None else self.max_ctx_len
        self.context_dim = input_shape[1][2]

        if self.stateful:
            self.reset_states()
        else:
            # initial states: all-zero tensor of shape (output_dim)
            self.states = [None]

        # Initialize Att model params (following the same format for any option of self.consume_less)
        self.wa = self.add_weight((self.context_dim, ),
                                   initializer=self.init_att,
                                   name='{}_wa'.format(self.name),
                                   regularizer=self.wa_regularizer)

        self.Wa = self.add_weight((self.output_dim, self.context_dim),
                                   initializer=self.init_att,
                                   name='{}_Wa'.format(self.name),
                                   regularizer=self.Wa_regularizer)
        self.Ua = self.add_weight((self.context_dim, self.context_dim),
                                   initializer=self.inner_init,
                                   name='{}_Ua'.format(self.name),
                                   regularizer=self.Ua_regularizer)

        self.ba = self.add_weight(self.context_dim,
                                   initializer='zero',
                                   name='{}_ba'.format(self.name),
                                   regularizer=self.ba_regularizer)

        self.ca = self.add_weight(self.context_steps,
                                  initializer='zero',
                                   name='{}_ca'.format(self.name),
                                  regularizer=self.ca_regularizer)

        if self.consume_less == 'gpu':
            self.V = self.add_weight((self.input_dim, 3 * self.output_dim),
                                     initializer=self.inner_init,
                                     name='{}_V'.format(self.name),
                                     regularizer=self.V_regularizer)
            self.W = self.add_weight((self.context_dim, 3 * self.output_dim),
                                     initializer=self.init,
                                     name='{}_W'.format(self.name),
                                     regularizer=self.W_regularizer)
            self.U = self.add_weight((self.output_dim, 3 * self.output_dim),
                                     initializer=self.inner_init,
                                     name='{}_U'.format(self.name),
                                     regularizer=self.U_regularizer)
            self.b = self.add_weight((self.output_dim * 3,),
                                     initializer='zero',
                                     name='{}_b'.format(self.name),
                                     regularizer=self.b_regularizer)

            self.trainable_weights =  [self.wa, self.Wa, self.Ua, self.ba, self.ca,  # AttModel parameters
                                       self.V, # Cond weights
                                       self.W, self.U, self.b]
        else:
            self.V_z = self.add_weight((self.input_dim, self.output_dim),
                                       initializer=self.inner_init,
                                       name='{}_V_z'.format(self.name),
                                       regularizer=self.W_regularizer)
            self.W_z = self.add_weight((self.context_dim, self.output_dim),
                                       initializer=self.init,
                                       name='{}_W_z'.format(self.name),
                                       regularizer=self.W_regularizer)
            self.U_z = self.add_weight((self.output_dim, self.output_dim),
                                       initializer=self.init,
                                       name='{}_U_z'.format(self.name),
                                       regularizer=self.W_regularizer)
            self.b_z = self.add_weight((self.output_dim,),
                                       initializer='zero',
                                       name='{}_b_z'.format(self.name),
                                       regularizer=self.b_regularizer)
            self.V_r = self.add_weight((self.input_dim, self.output_dim),
                                       initializer=self.inner_init,
                                       name='{}_V_r'.format(self.name),
                                       regularizer=self.W_regularizer)
            self.W_r = self.add_weight((self.context_dim, self.output_dim),
                                       initializer=self.init,
                                       name='{}_W_r'.format(self.name),
                                       regularizer=self.W_regularizer)
            self.U_r = self.add_weight((self.output_dim, self.output_dim),
                                       initializer=self.init,
                                       name='{}_U_r'.format(self.name),
                                       regularizer=self.W_regularizer)
            self.b_r = self.add_weight((self.output_dim,),
                                       initializer='zero',
                                       name='{}_b_r'.format(self.name),
                                       regularizer=self.b_regularizer)
            self.V_h = self.add_weight((self.input_dim, self.output_dim),
                                       initializer=self.inner_init,
                                       name='{}_V_h'.format(self.name),
                                       regularizer=self.W_regularizer)
            self.W_h = self.add_weight((self.context_dim, self.output_dim),
                                       initializer=self.init,
                                       name='{}_W_h'.format(self.name),
                                       regularizer=self.W_regularizer)
            self.U_h = self.add_weight((self.output_dim, self.output_dim),
                                       initializer=self.init,
                                       name='{}_U_h'.format(self.name),
                                       regularizer=self.W_regularizer)
            self.b_h = self.add_weight((self.output_dim,),
                                       initializer='zero',
                                       name='{}_b_h'.format(self.name),
                                       regularizer=self.b_regularizer)

            self.trainable_weights = [self.wa, self.Wa, self.Ua, self.ba, self.ca,  # AttModel parameters
                                      self.V_z, self.W_r, self.U_h, self.b_z,
                                      self.V_r, self.W_r, self.U_r, self.b_r,
                                      self.V_h, self.W_h, self.U_h, self.b_h
                                      ]

            self.V = K.concatenate([self.V_z, self.V_r, self.V_h])
            self.W = K.concatenate([self.W_z, self.W_r, self.W_h])
            self.U = K.concatenate([self.U_z, self.U_r, self.U_h])
            self.b = K.concatenate([self.b_z, self.b_r, self.b_h])

        if self.initial_weights is not None:
            self.set_weights(self.initial_weights)
            del self.initial_weights
        self.built = True

    def reset_states(self):
        assert self.stateful, 'Layer must be stateful.'
        input_shape = self.input_spec[0].shape
        if not input_shape[0]:
            raise Exception('If a RNN is stateful, a complete ' +
                            'input_shape must be provided (including batch size).')
        if hasattr(self, 'states'):
            K.set_value(self.states[0],
                        np.zeros((input_shape[0], self.output_dim)))
            K.set_value(self.states[1],
                        np.zeros((input_shape[0], self.output_dim)))
            K.set_value(self.states[2],
                        np.zeros((input_shape[0], input_shape[3])))
        else:
            self.states = [K.zeros((input_shape[0], self.output_dim)),
               K.zeros((input_shape[0], self.output_dim)),
               K.zeros((input_shape[0], input_shape[3]))]

    def preprocess_input(self, x, B_V):
            return K.dot(x * B_V[0], self.V) + self.b

    def get_output_shape_for(self, input_shape):
        if self.return_sequences:
            main_out = (input_shape[0][0], input_shape[0][1], self.output_dim)
        else:
            main_out = (input_shape[0][0], self.output_dim)

        if self.return_extra_variables:
            dim_x_att = (input_shape[0][0], input_shape[0][1], self.context_dim)
            dim_alpha_att = (input_shape[0][0], input_shape[0][1], input_shape[1][1])
            main_out = [main_out, dim_x_att, dim_alpha_att]

        if self.return_states:
            if not isinstance(main_out, list):
                main_out = [main_out]
            states_dim = (input_shape[0][0], input_shape[0][1], self.output_dim)
            main_out += [states_dim]

        return main_out

    def call(self, x, mask=None):
        # input shape: (nb_samples, time (padded with zeros), input_dim)
        # note that the .build() method of subclasses MUST define
        # self.input_spec with a complete input shape.

        input_shape = self.input_spec[0].shape
        state_below = x[0]
        self.context = x[1]
        if self.num_inputs == 2:  # input: [state_below, context]
            self.init_state = None
            self.init_memory = None
        elif self.num_inputs == 3:  # input: [state_below, context, init_hidden_state]
            self.init_state = x[2]
            self.init_memory = None
        if K._BACKEND == 'tensorflow':
            if not input_shape[1]:
                raise Exception('When using TensorFlow, you should define '
                                'explicitly the number of timesteps of '
                                'your sequences.\n'
                                'If your first layer is an Embedding, '
                                'make sure to pass it an "input_length" '
                                'argument. Otherwise, make sure '
                                'the first layer has '
                                'an "input_shape" or "batch_input_shape" '
                                'argument, including the time axis. '
                                'Found input shape at layer ' + self.name +
                                ': ' + str(input_shape))
        if self.stateful:
            initial_states = self.states
        else:
            initial_states = self.get_initial_states(state_below)

        constants, B_V = self.get_constants(state_below, mask[1])
        preprocessed_input = self.preprocess_input(state_below, B_V)
        last_output, outputs, states = K.rnn(self.step, preprocessed_input,
                                             initial_states,
                                             go_backwards=self.go_backwards,
                                             mask=mask[0],
                                             constants=constants,
                                             unroll=self.unroll,
                                             input_length=state_below.shape[1],
                                             pos_extra_outputs_states=[1, 2])
        if self.stateful:
            self.updates = []
            for i in range(len(states)):
                self.updates.append((self.states[i], states[i]))

        if self.return_sequences:
            ret = outputs
        else:
            ret = last_output

        if self.return_extra_variables:
            ret = [ret, states[1], states[2]]

        # intermediate states as additional outputs
        if self.return_states:
            if not isinstance(ret, list):
                ret = [ret]
            ret += [states[0]]

        return ret

    def compute_mask(self, input, mask):
        if self.return_extra_variables:
            ret = [mask[0], mask[0], mask[0]]
        else:
            ret = mask[0]

        if self.return_states:
            if not isinstance(ret, list):
                ret = [ret]
            ret += [mask[0]]

        return ret

    def step(self, x, states):
        h_tm1 = states[0]  # previous memory
        non_used_x_att = states[1]  # Placeholder for returning extra variables
        non_used_alphas_att = states[2]  # Placeholder for returning extra variables
        # dropout matrices for recurrent units
        B_U = states[3]     # Dropout U
        B_W = states[4]     # Dropout W
        # Att model dropouts
        B_wa = states[5]
        B_Wa = states[6]

        # Context (input sequence)
        pctx_ = states[7]       # Projected context (i.e. context * Ua + ba)
        context = states[8]     # Original context
        mask_input = states[9]  # Context mask

        if mask_input.ndim > 1: # Mask the context (only if necessary)
            pctx_ = mask_input[:, :, None] * pctx_
            context = mask_input[:, :, None] * context    # Masked context

        # AttModel (see Formulation in class header)
        p_state_ = K.dot(h_tm1 * B_Wa[0], self.Wa)
        pctx_ = K.tanh(pctx_ +  p_state_[:, None, :])
        e = K.dot(pctx_ * B_wa[0], self.wa) + self.ca
        if mask_input.ndim > 1: # Mask the context (only if necessary)
            e = mask_input * e
        alphas_shape = e.shape
        alphas = K.softmax(e.reshape([alphas_shape[0], alphas_shape[1]]))
        ctx_ = (context * alphas[:, :, None]).sum(axis=1) # sum over the in_timesteps dimension resulting in [batch_size, input_dim]

        if self.consume_less == 'gpu':
            matrix_x = x + K.dot(ctx_ * B_W[0], self.W)
            matrix_inner = K.dot(h_tm1 * B_U[0], self.U[:, :2 * self.output_dim])

            x_z = matrix_x[:, :self.output_dim]
            x_r = matrix_x[:, self.output_dim: 2 * self.output_dim]
            inner_z = matrix_inner[:, :self.output_dim]
            inner_r = matrix_inner[:, self.output_dim: 2 * self.output_dim]

            z = self.inner_activation(x_z + inner_z)
            r = self.inner_activation(x_r + inner_r)

            x_h = matrix_x[:, 2 * self.output_dim:]
            inner_h = K.dot(r * h_tm1 * B_U[0], self.U[:, 2 * self.output_dim:])
            hh = self.activation(x_h + inner_h)

        h = z * h_tm1 + (1 - z) * hh

        return h, [h, ctx_, alphas]

    def get_constants(self, x, mask_input):
        constants = []
        # States[3]
        if 0 < self.dropout_U < 1:
            ones = K.ones_like(K.reshape(x[:, 0, 0], (-1, 1)))
            ones = K.concatenate([ones] * self.output_dim, 1)
            B_U = [K.in_train_phase(K.dropout(ones, self.dropout_U), ones) for _ in range(3)]
            constants.append(B_U)
        else:
            constants.append([K.cast_to_floatx(1.) for _ in range(3)])

        # States[4]
        if 0 < self.dropout_W < 1:
            input_shape = self.input_spec[0][0].shape
            input_dim = input_shape[-1]
            ones = K.ones_like(K.reshape(x[:, 0, 0], (-1, 1)))
            ones = K.concatenate([ones] * input_dim, 1)
            B_W = [K.in_train_phase(K.dropout(ones, self.dropout_W), ones) for _ in range(3)]
            constants.append(B_W)
        else:
            constants.append([K.cast_to_floatx(1.) for _ in range(3)])

        if 0 < self.dropout_V < 1:
            input_dim = self.input_dim
            ones = K.ones_like(K.reshape(x[:, :, 0], (-1, x.shape[1], 1))) # (bs, timesteps, 1)
            ones = K.concatenate([ones] * input_dim, axis=2)
            B_V = [K.in_train_phase(K.dropout(ones, self.dropout_V), ones) for _ in range(3)]
        else:
            B_V = [K.cast_to_floatx(1.) for _ in range(3)]

        # AttModel
        # States[5]
        if 0 < self.dropout_wa < 1:
            ones = K.ones_like(K.reshape(self.context[:, :, 0], (-1, self.context.shape[1], 1)))
            #ones = K.concatenate([ones], 1)
            B_wa = [K.in_train_phase(K.dropout(ones, self.dropout_wa), ones)]
            constants.append(B_wa)
        else:
            constants.append([K.cast_to_floatx(1.)])

        # States[6]
        if 0 < self.dropout_Wa < 1:
            input_dim = self.output_dim
            ones = K.ones_like(K.reshape(x[:, 0, 0], (-1, 1)))
            ones = K.concatenate([ones] * input_dim, 1)
            B_Wa = [K.in_train_phase(K.dropout(ones, self.dropout_Wa), ones)]
            constants.append(B_Wa)
        else:
            constants.append([K.cast_to_floatx(1.)])

        if 0 < self.dropout_Ua < 1:
            input_dim = self.context_dim
            ones = K.ones_like(K.reshape(self.context[:, :, 0], (-1, self.context.shape[1], 1)))
            ones = K.concatenate([ones] * input_dim, axis=2)
            B_Ua = [K.in_train_phase(K.dropout(ones, self.dropout_Ua), ones)]
            pctx = K.dot(self.context * B_Ua[0], self.Ua) + self.ba
        else:
            pctx = K.dot(self.context, self.Ua) + self.ba

        # States[7]
        constants.append(pctx)

        # States[8]
        constants.append(self.context)

        # States[9]
        if mask_input is None:
            mask_input = K.not_equal(K.sum(self.context, axis=2), self.mask_value)
        constants.append(mask_input)

        return constants, B_V

    def get_initial_states(self, x):
        # build an all-zero tensor of shape (samples, output_dim)
        if self.init_state is None:
            # build an all-zero tensor of shape (samples, output_dim)
            initial_state = K.zeros_like(x)  # (samples, timesteps, input_dim)
            initial_state = K.sum(initial_state, axis=(1, 2))  # (samples,)
            initial_state = K.expand_dims(initial_state)  # (samples, 1)
            initial_state = K.tile(initial_state, [1, self.output_dim]) # (samples, output_dim)
        else:
            initial_state = self.init_state

        initial_states = [initial_state]

        initial_state = K.zeros_like(self.context)            # (samples, intput_timesteps, ctx_dim)
        initial_state_alphas = K.sum(initial_state, axis=2)   # (samples, input_timesteps)
        initial_state = K.sum(initial_state, axis=1)          # (samples, ctx_dim)
        extra_states = [initial_state, initial_state_alphas]  # (samples, ctx_dim)

        return initial_states + extra_states

    def get_config(self):
        config = {'output_dim': self.output_dim,
                  'return_extra_variables': self.return_extra_variables,
                  'return_states': self.return_states,
                  'init': self.init.__name__,
                  'inner_init': self.inner_init.__name__,
                  'activation': self.activation.__name__,
                  'inner_activation': self.inner_activation.__name__,
                  'mask_value': self.mask_value,
                  'W_regularizer': self.W_regularizer.get_config() if self.W_regularizer else None,
                  'U_regularizer': self.U_regularizer.get_config() if self.U_regularizer else None,
                  'V_regularizer': self.V_regularizer.get_config() if self.V_regularizer else None,
                  'b_regularizer': self.b_regularizer.get_config() if self.b_regularizer else None,
                  'wa_regularizer': self.wa_regularizer.get_config() if self.wa_regularizer else None,
                  'Wa_regularizer': self.Wa_regularizer.get_config() if self.Wa_regularizer else None,
                  'Ua_regularizer': self.Ua_regularizer.get_config() if self.Ua_regularizer else None,
                  'ba_regularizer': self.ba_regularizer.get_config() if self.ba_regularizer else None,
                  'ca_regularizer': self.ca_regularizer.get_config() if self.ca_regularizer else None,
                  'dropout_W': self.dropout_W,
                  'dropout_U': self.dropout_U,
                  'dropout_V': self.dropout_V,
                  'dropout_wa': self.dropout_wa,
                  'dropout_Wa': self.dropout_Wa,
                  'dropout_Ua': self.dropout_Ua}
        base_config = super(AttGRUCond, self).get_config()
        return dict(list(base_config.items()) + list(config.items()))


class LSTM(Recurrent):
    '''Long-Short Term Memory unit - Hochreiter 1997.

    For a step-by-step description of the algorithm, see
    [this tutorial](http://deeplearning.net/tutorial/lstm.html).

    # Arguments
        output_dim: dimension of the internal projections and the final output.
        init: weight initialization function.
            Can be the name of an existing function (str),
            or a Theano function (see: [initializations](../initializations.md)).
        inner_init: initialization function of the inner cells.
        forget_bias_init: initialization function for the bias of the forget gate.
            [Jozefowicz et al.](http://www.jmlr.org/proceedings/papers/v37/jozefowicz15.pdf)
            recommend initializing with ones.
        activation: activation function.
            Can be the name of an existing function (str),
            or a Theano function (see: [activations](../activations.md)).
        inner_activation: activation function for the inner cells.
        W_regularizer: instance of [WeightRegularizer](../regularizers.md)
            (eg. L1 or L2 regularization), applied to the input weights matrices.
        U_regularizer: instance of [WeightRegularizer](../regularizers.md)
            (eg. L1 or L2 regularization), applied to the recurrent weights matrices.
        b_regularizer: instance of [WeightRegularizer](../regularizers.md),
            applied to the bias.
        dropout_W: float between 0 and 1. Fraction of the input units to drop for input gates.
        dropout_U: float between 0 and 1. Fraction of the input units to drop for recurrent connections.

    # References
        - [Long short-term memory](http://deeplearning.cs.cmu.edu/pdfs/Hochreiter97_lstm.pdf) (original 1997 paper)
        - [Learning to forget: Continual prediction with LSTM](http://www.mitpressjournals.org/doi/pdf/10.1162/089976600300015015)
        - [Supervised sequence labeling with recurrent neural networks](http://www.cs.toronto.edu/~graves/preprint.pdf)
        - [A Theoretically Grounded Application of Dropout in Recurrent Neural Networks](http://arxiv.org/abs/1512.05287)
    '''
    def __init__(self, output_dim,
                 init='glorot_uniform', inner_init='orthogonal',
                 forget_bias_init='one', activation='tanh',
                 inner_activation='hard_sigmoid',
                 W_regularizer=None, U_regularizer=None, b_regularizer=None,
                 dropout_W=0., dropout_U=0., **kwargs):
        self.output_dim = output_dim
        self.init = initializations.get(init)
        self.inner_init = initializations.get(inner_init)
        self.forget_bias_init = initializations.get(forget_bias_init)
        self.activation = activations.get(activation)
        self.inner_activation = activations.get(inner_activation)
        self.W_regularizer = regularizers.get(W_regularizer)
        self.U_regularizer = regularizers.get(U_regularizer)
        self.b_regularizer = regularizers.get(b_regularizer)
        self.dropout_W = dropout_W
        self.dropout_U = dropout_U

        if self.dropout_W or self.dropout_U:
            self.uses_learning_phase = True
        super(LSTM, self).__init__(**kwargs)

    def build(self, input_shape):
        self.input_spec = [InputSpec(shape=input_shape)]
        self.input_dim = input_shape[2]

        if self.stateful:
            self.reset_states()
        else:
            # initial states: 2 all-zero tensors of shape (output_dim)
            self.states = [None, None]

        if self.consume_less == 'gpu':
            self.W = self.add_weight((self.input_dim, 4 * self.output_dim),
                                     initializer=self.init,
                                     name='{}_W'.format(self.name),
                                     regularizer=self.W_regularizer)
            self.U = self.add_weight((self.output_dim, 4 * self.output_dim),
                                     initializer=self.inner_init,
                                     name='{}_U'.format(self.name),
                                     regularizer=self.U_regularizer)

            def b_reg(shape, name=None):
                return K.variable(np.hstack((np.zeros(self.output_dim),
                                             K.get_value(self.forget_bias_init((self.output_dim,))),
                                             np.zeros(self.output_dim),
                                             np.zeros(self.output_dim))),
                                  name='{}_b'.format(self.name))
            self.b = self.add_weight((self.output_dim * 4,),
                                     initializer=b_reg,
                                     name='{}_b'.format(self.name),
                                     regularizer=self.b_regularizer)
        else:
            self.W_i = self.add_weight((self.input_dim, self.output_dim),
                                       initializer=self.init,
                                       name='{}_W_i'.format(self.name),
                                       regularizer=self.W_regularizer)
            self.U_i = self.add_weight((self.output_dim, self.output_dim),
                                       initializer=self.init,
                                       name='{}_U_i'.format(self.name),
                                       regularizer=self.W_regularizer)
            self.b_i = self.add_weight((self.output_dim,),
                                       initializer='zero',
                                       name='{}_b_i'.format(self.name),
                                       regularizer=self.b_regularizer)
            self.W_f = self.add_weight((self.input_dim, self.output_dim),
                                       initializer=self.init,
                                       name='{}_W_f'.format(self.name),
                                       regularizer=self.W_regularizer)
            self.U_f = self.add_weight((self.output_dim, self.output_dim),
                                       initializer=self.init,
                                       name='{}_U_f'.format(self.name),
                                       regularizer=self.W_regularizer)
            self.b_f = self.add_weight((self.output_dim,),
                                       initializer=self.forget_bias_init,
                                       name='{}_b_f'.format(self.name),
                                       regularizer=self.b_regularizer)
            self.W_c = self.add_weight((self.input_dim, self.output_dim),
                                       initializer=self.init,
                                       name='{}_W_c'.format(self.name),
                                       regularizer=self.W_regularizer)
            self.U_c = self.add_weight((self.output_dim, self.output_dim),
                                       initializer=self.init,
                                       name='{}_U_c'.format(self.name),
                                       regularizer=self.W_regularizer)
            self.b_c = self.add_weight((self.output_dim,),
                                       initializer='zero',
                                       name='{}_b_c'.format(self.name),
                                       regularizer=self.b_regularizer)
            self.W_o = self.add_weight((self.input_dim, self.output_dim),
                                       initializer=self.init,
                                       name='{}_W_o'.format(self.name),
                                       regularizer=self.W_regularizer)
            self.U_o = self.add_weight((self.output_dim, self.output_dim),
                                       initializer=self.init,
                                       name='{}_U_o'.format(self.name),
                                       regularizer=self.W_regularizer)
            self.b_o = self.add_weight((self.output_dim,),
                                       initializer='zero',
                                       name='{}_b_o'.format(self.name),
                                       regularizer=self.b_regularizer)

            self.trainable_weights = [self.W_i, self.U_i, self.b_i,
                                      self.W_c, self.U_c, self.b_c,
                                      self.W_f, self.U_f, self.b_f,
                                      self.W_o, self.U_o, self.b_o]
            self.W = K.concatenate([self.W_i, self.W_f, self.W_c, self.W_o])
            self.U = K.concatenate([self.U_i, self.U_f, self.U_c, self.U_o])
            self.b = K.concatenate([self.b_i, self.b_f, self.b_c, self.b_o])

        if self.initial_weights is not None:
            self.set_weights(self.initial_weights)
            del self.initial_weights
        self.built = True

    def reset_states(self):
        assert self.stateful, 'Layer must be stateful.'
        input_shape = self.input_spec[0].shape
        if not input_shape[0]:
            raise ValueError('If a RNN is stateful, a complete ' +
                             'input_shape must be provided (including batch size).')
        if hasattr(self, 'states'):
            K.set_value(self.states[0],
                        np.zeros((input_shape[0], self.output_dim)))
            K.set_value(self.states[1],
                        np.zeros((input_shape[0], self.output_dim)))
        else:
            self.states = [K.zeros((input_shape[0], self.output_dim)),
                           K.zeros((input_shape[0], self.output_dim))]

    def preprocess_input(self, x):
        if self.consume_less == 'cpu':
            if 0 < self.dropout_W < 1:
                dropout = self.dropout_W
            else:
                dropout = 0
            input_shape = K.int_shape(x)
            input_dim = input_shape[2]
            timesteps = input_shape[1]

            x_i = time_distributed_dense(x, self.W_i, self.b_i, dropout,
                                         input_dim, self.output_dim, timesteps)
            x_f = time_distributed_dense(x, self.W_f, self.b_f, dropout,
                                         input_dim, self.output_dim, timesteps)
            x_c = time_distributed_dense(x, self.W_c, self.b_c, dropout,
                                         input_dim, self.output_dim, timesteps)
            x_o = time_distributed_dense(x, self.W_o, self.b_o, dropout,
                                         input_dim, self.output_dim, timesteps)
            return K.concatenate([x_i, x_f, x_c, x_o], axis=2)
        else:
            return x

    def step(self, x, states):
        h_tm1 = states[0]
        c_tm1 = states[1]
        B_U = states[2]
        B_W = states[3]

        if self.consume_less == 'gpu':
            z = K.dot(x * B_W[0], self.W) + K.dot(h_tm1 * B_U[0], self.U) + self.b

            z0 = z[:, :self.output_dim]
            z1 = z[:, self.output_dim: 2 * self.output_dim]
            z2 = z[:, 2 * self.output_dim: 3 * self.output_dim]
            z3 = z[:, 3 * self.output_dim:]

            i = self.inner_activation(z0)
            f = self.inner_activation(z1)
            c = f * c_tm1 + i * self.activation(z2)
            o = self.inner_activation(z3)
        else:
            if self.consume_less == 'cpu':
                x_i = x[:, :self.output_dim]
                x_f = x[:, self.output_dim: 2 * self.output_dim]
                x_c = x[:, 2 * self.output_dim: 3 * self.output_dim]
                x_o = x[:, 3 * self.output_dim:]
            elif self.consume_less == 'mem':
                x_i = K.dot(x * B_W[0], self.W_i) + self.b_i
                x_f = K.dot(x * B_W[1], self.W_f) + self.b_f
                x_c = K.dot(x * B_W[2], self.W_c) + self.b_c
                x_o = K.dot(x * B_W[3], self.W_o) + self.b_o
            else:
                raise ValueError('Unknown `consume_less` mode.')

            i = self.inner_activation(x_i + K.dot(h_tm1 * B_U[0], self.U_i))
            f = self.inner_activation(x_f + K.dot(h_tm1 * B_U[1], self.U_f))
            c = f * c_tm1 + i * self.activation(x_c + K.dot(h_tm1 * B_U[2], self.U_c))
            o = self.inner_activation(x_o + K.dot(h_tm1 * B_U[3], self.U_o))

        h = o * self.activation(c)
        return h, [h, c]

    def get_constants(self, x):
        constants = []
        if 0 < self.dropout_U < 1:
            ones = K.ones_like(K.reshape(x[:, 0, 0], (-1, 1)))
            ones = K.tile(ones, (1, self.output_dim))
            B_U = [K.in_train_phase(K.dropout(ones, self.dropout_U), ones) for _ in range(4)]
            constants.append(B_U)
        else:
            constants.append([K.cast_to_floatx(1.) for _ in range(4)])

        if 0 < self.dropout_W < 1:
            input_shape = K.int_shape(x)
            input_dim = input_shape[-1]
            ones = K.ones_like(K.reshape(x[:, 0, 0], (-1, 1)))
            ones = K.tile(ones, (1, int(input_dim)))
            B_W = [K.in_train_phase(K.dropout(ones, self.dropout_W), ones) for _ in range(4)]
            constants.append(B_W)
        else:
            constants.append([K.cast_to_floatx(1.) for _ in range(4)])
        return constants

    def get_config(self):
        config = {'output_dim': self.output_dim,
                  'init': self.init.__name__,
                  'inner_init': self.inner_init.__name__,
                  'forget_bias_init': self.forget_bias_init.__name__,
                  'activation': self.activation.__name__,
                  'inner_activation': self.inner_activation.__name__,
                  'W_regularizer': self.W_regularizer.get_config() if self.W_regularizer else None,
                  'U_regularizer': self.U_regularizer.get_config() if self.U_regularizer else None,
                  'b_regularizer': self.b_regularizer.get_config() if self.b_regularizer else None,
                  'dropout_W': self.dropout_W,
                  'dropout_U': self.dropout_U}
        base_config = super(LSTM, self).get_config()
        return dict(list(base_config.items()) + list(config.items()))

class LSTMCond(Recurrent):
    '''Conditional LSTM: The previously generated word is fed to the current timestep

    # Arguments
        output_dim: dimension of the internal projections and the final output.
        init: weight initialization function.
            Can be the name of an existing function (str),
            or a Theano function (see: [initializations](../initializations.md)).
        inner_init: initialization function of the inner cells.
        return_states: boolean indicating if we want the intermediate states (hidden_state and memory) as additional outputs
        forget_bias_init: initialization function for the bias of the forget gate.
            [Jozefowicz et al.](http://www.jmlr.org/proceedings/papers/v37/jozefowicz15.pdf)
            recommend initializing with ones.
        activation: activation function.
            Can be the name of an existing function (str),
            or a Theano function (see: [activations](../activations.md)).
        inner_activation: activation function for the inner cells.
        W_regularizer: instance of [WeightRegularizer](../regularizers.md)
            (eg. L1 or L2 regularization), applied to the input weights matrices.
        U_regularizer: instance of [WeightRegularizer](../regularizers.md)
            (eg. L1 or L2 regularization), applied to the recurrent weights matrices.
        b_regularizer: instance of [WeightRegularizer](../regularizers.md),
            applied to the bias.
        dropout_W: float between 0 and 1. Fraction of the input units to drop for input gates.
        dropout_U: float between 0 and 1. Fraction of the input units to drop for recurrent connections.

    # References
        - [Long short-term memory](http://deeplearning.cs.cmu.edu/pdfs/Hochreiter97_lstm.pdf) (original 1997 paper)
        - [Learning to forget: Continual prediction with LSTM](http://www.mitpressjournals.org/doi/pdf/10.1162/089976600300015015)
        - [Supervised sequence labelling with recurrent neural networks](http://www.cs.toronto.edu/~graves/preprint.pdf)
        - [A Theoretically Grounded Application of Dropout in Recurrent Neural Networks](http://arxiv.org/abs/1512.05287)
    '''
    def __init__(self, output_dim,
                 init='glorot_uniform', inner_init='orthogonal',
                 return_states=False,
                 forget_bias_init='one', activation='tanh',
                 inner_activation='hard_sigmoid',
                 W_regularizer=None, V_regularizer=None, U_regularizer=None, b_regularizer=None,
                 dropout_W=0., dropout_U=0., dropout_V=0., **kwargs):
        self.output_dim = output_dim
        self.init = initializations.get(init)
        self.inner_init = initializations.get(inner_init)
        self.return_states = return_states
        self.forget_bias_init = initializations.get(forget_bias_init)
        self.activation = activations.get(activation)
        self.inner_activation = activations.get(inner_activation)
        self.W_regularizer = regularizers.get(W_regularizer)
        self.V_regularizer = regularizers.get(V_regularizer)
        self.U_regularizer = regularizers.get(U_regularizer)
        self.b_regularizer = regularizers.get(b_regularizer)
        self.dropout_W, self.dropout_U, self.dropout_V = dropout_W, dropout_U, dropout_V

        if self.dropout_W or self.dropout_U or self.dropout_V:
            self.uses_learning_phase = True

        super(LSTMCond, self).__init__(**kwargs)

    def build(self, input_shape):

        assert len(input_shape) == 2 or len(input_shape) == 4, 'You should pass two inputs to LSTMCond ' \
                                                               '(context and previous_embedded_words) and ' \
                                                               'two optional inputs (init_state and init_memory)'

        if len(input_shape) == 2:
            self.input_spec = [InputSpec(shape=input_shape[0]), InputSpec(shape=input_shape[1])]
            self.num_inputs = 2
        elif len(input_shape) == 4:
            self.input_spec = [InputSpec(shape=input_shape[0]), InputSpec(shape=input_shape[1]),
                               InputSpec(shape=input_shape[2]), InputSpec(shape=input_shape[3])]
            self.num_inputs = 4
        self.input_dim = input_shape[0][2]
        if self.input_spec[1].ndim == 3:
            self.context_dim = input_shape[1][2]
            self.static_ctx = False
            assert input_shape[1][1] == input_shape[0][1], 'When using a 3D ctx in LSTMCond, it has to have the same ' \
                                                          'number of timesteps (dimension 1) as the input. Currently,' \
                                                          'the number of input timesteps is: ' \
                                                           + str(input_shape[0][1]) + \
                                                          ', while the number of ctx timesteps is ' \
                                                           + str(input_shape[1][1]) + ' (complete shapes: '\
                                                           + str(input_shape[0]) + ', ' + str(input_shape[1]) + ')'
        else:
            self.context_dim = input_shape[1][1]
            self.static_ctx = True

        if self.stateful:
            self.reset_states()
        else:
            # initial states: 2 all-zero tensors of shape (output_dim)
            self.states = [None, None] # [h, c]

        if self.consume_less == 'gpu':
            self.W = self.add_weight((self.context_dim, 4 * self.output_dim),
                                     initializer=self.init,
                                     name='{}_W'.format(self.name),
                                     regularizer=self.W_regularizer)
            self.U = self.add_weight((self.output_dim, 4 * self.output_dim),
                                     initializer=self.inner_init,
                                     name='{}_U'.format(self.name),
                                     regularizer=self.U_regularizer)
            self.V = self.add_weight((self.input_dim, 4 * self.output_dim),
                                      initializer=self.init,
                                     name='{}_V'.format(self.name),
                                     regularizer=self.V_regularizer)
            def b_reg(shape, name=None):
                return K.variable(np.hstack((np.zeros(self.output_dim),
                                             K.get_value(self.forget_bias_init((self.output_dim,))),
                                             np.zeros(self.output_dim),
                                             np.zeros(self.output_dim))),
                                  name='{}_b'.format(self.name))

            self.b = self.add_weight((self.output_dim * 4,),
                                     initializer=b_reg,
                                     name='{}_b'.format(self.name),
                                     regularizer=self.b_regularizer)
            self.trainable_weights = [self.W,
                                      self.U,
                                      self.V,
                                      self.b]

        else:
            self.V_i = self.add_weight((self.input_dim, self.output_dim),
                                 initializer=self.init,
                                 name='{}_V_i'.format(self.name),
                                 regularizer=self.W_regularizer)
            self.W_i = self.add_weight((self.context_dim, self.output_dim),
                                 initializer=self.init,
                                 name='{}_W_i'.format(self.name),
                                 regularizer=self.W_regularizer)
            self.U_i = self.add_weight((self.output_dim, self.output_dim),
                                       initializer=self.inner_init,
                                       name='{}_U_i'.format(self.name),
                                       regularizer=self.W_regularizer)
            self.b_i = self.add_weight((self.output_dim,),
                                       initializer='zero',
                                       name='{}_b_i'.format(self.name),
                                       regularizer=self.b_regularizer)
            self.V_f = self.add_weight((self.input_dim, self.output_dim),
                                 initializer=self.init,
                                 name='{}_V_f'.format(self.name),
                                 regularizer=self.W_regularizer)
            self.W_f = self.add_weight((self.context_dim, self.output_dim),
                                 initializer=self.init,
                                 name='{}_W_f'.format(self.name),
                                 regularizer=self.W_regularizer)
            self.U_f = self.add_weight((self.output_dim, self.output_dim),
                                       initializer=self.inner_init,
                                       name='{}_U_f'.format(self.name),
                                       regularizer=self.W_regularizer)
            self.b_f = self.add_weight((self.output_dim,),
                                       initializer=self.forget_bias_init,
                                       name='{}_b_f'.format(self.name),
                                       regularizer=self.b_regularizer)
            self.V_c = self.add_weight((self.input_dim, self.output_dim),
                                 initializer=self.init,
                                 name='{}_V_c'.format(self.name),
                                 regularizer=self.W_regularizer)
            self.W_c = self.add_weight((self.context_dim, self.output_dim),
                                 initializer=self.init,
                                 name='{}_W_c'.format(self.name),
                                 regularizer=self.W_regularizer)
            self.U_c = self.add_weight((self.output_dim, self.output_dim),
                                       initializer=self.inner_init,
                                       name='{}_U_c'.format(self.name),
                                       regularizer=self.W_regularizer)
            self.b_c = self.add_weight((self.output_dim,),
                                       initializer='zero',
                                       name='{}_b_c'.format(self.name),
                                       regularizer=self.b_regularizer)
            self.V_o = self.add_weight((self.input_dim, self.output_dim),
                                 initializer=self.init,
                                 name='{}_V_o'.format(self.name),
                                 regularizer=self.W_regularizer)
            self.W_o = self.add_weight((self.context_dim, self.output_dim),
                                 initializer='zero',
                                 name='{}_W_o'.format(self.name),
                                 regularizer=self.W_regularizer)
            self.U_o = self.add_weight((self.output_dim, self.output_dim),
                                       initializer=self.inner_init,
                                       name='{}_U_o'.format(self.name),
                                       regularizer=self.W_regularizer)
            self.b_o = self.add_weight((self.output_dim,),
                                       initializer='zero',
                                       name='{}_b_o'.format(self.name),
                                       regularizer=self.b_regularizer)
            self.V_x = self.add_weight((self.output_dim, self.input_dim),
                                 initializer=self.init,
                                 name='{}_V_x'.format(self.name),
                                 regularizer=self.W_regularizer)
            self.W_x = self.add_weight((self.output_dim, self.context_dim),
                                 initializer=self.init,
                                 name='{}_W_x'.format(self.name),
                                 regularizer=self.W_regularizer)
            self.b_x = self.add_weight((self.output_dim,),
                                       initializer='zero',
                                       name='{}_b_x'.format(self.name),
                                       regularizer=self.b_regularizer)
            self.trainable_weights = [self.V_i, self.W_i, self.U_i, self.b_i,
                                      self.V_c, self.W_c, self.U_c, self.b_c,
                                      self.V_f, self.W_f, self.U_f, self.b_f,
                                      self.V_o, self.W_o, self.U_o, self.b_o,
                                      self.V_x, self.W_x, self.b_x
                                      ]

            self.W = K.concatenate([self.W_i, self.W_f, self.W_c, self.W_o])
            self.U = K.concatenate([self.U_i, self.U_f, self.U_c, self.U_o])
            self.V = K.concatenate([self.V_i, self.V_f, self.V_c, self.V_o])
            self.b = K.concatenate([self.b_i, self.b_f, self.b_c, self.b_o])

        if self.initial_weights is not None:
            self.set_weights(self.initial_weights)
            del self.initial_weights
        self.built = True

    def reset_states(self):
        assert self.stateful, 'Layer must be stateful.'
        input_shape = self.input_shape
        if not input_shape[0]:
            raise Exception('If a RNN is stateful, a complete ' +
                            'input_shape must be provided (including batch size).')
        if hasattr(self, 'states'):
            K.set_value(self.states[0],
                        np.zeros((input_shape[0], self.output_dim)))
            K.set_value(self.states[1],
                        np.zeros((input_shape[0], self.output_dim)))
            K.set_value(self.states[2],
                        np.zeros((input_shape[0], input_shape[3])))
        else:
            self.states = [K.zeros((input_shape[0], self.output_dim)),
                           K.zeros((input_shape[0], self.output_dim))]#,

    def preprocess_input(self, x, context, dropouts):
        if self.static_ctx:
            return K.dot(x * dropouts[0][0], self.V)
        else:
            return K.dot(context * dropouts[0][0], self.W) + K.dot(x * dropouts[1][0], self.V)

    def get_output_shape_for(self, input_shape):
        if self.return_sequences:
            main_out = (input_shape[0][0], input_shape[0][1], self.output_dim)
        else:
            main_out = (input_shape[0][0], self.output_dim)

        if self.return_states:
            states_dim = (input_shape[0][0], input_shape[0][1], self.output_dim)
            main_out = [main_out, states_dim, states_dim]

        return main_out

    def call(self, x, mask=None):
        # input shape: (nb_samples, time (padded with zeros), input_dim)
        # note that the .build() method of subclasses MUST define
        # self.input_spec with a complete input shape.

        input_shape = self.input_spec[0].shape
        state_below = x[0]
        self.context = x[1]
        if self.num_inputs == 2: # input: [state_below, context]
            self.init_state = None
            self.init_memory = None
        elif self.num_inputs == 3: # input: [state_below, context, init_generic]
            self.init_state = x[2]
            self.init_memory = x[2]
        elif self.num_inputs == 4: # input: [state_below, context, init_state, init_memory]
            self.init_state = x[2]
            self.init_memory = x[3]
        if K._BACKEND == 'tensorflow':
            if not input_shape[1]:
                raise Exception('When using TensorFlow, you should define '
                                'explicitly the number of timesteps of '
                                'your sequences.\n'
                                'If your first layer is an Embedding, '
                                'make sure to pass it an "input_length" '
                                'argument. Otherwise, make sure '
                                'the first layer has '
                                'an "input_shape" or "batch_input_shape" '
                                'argument, including the time axis. '
                                'Found input shape at layer ' + self.name +
                                ': ' + str(input_shape))
        if self.stateful:
            initial_states = self.states
        else:
            initial_states = self.get_initial_states(state_below)
        constants, dropouts = self.get_constants(state_below)
        preprocessed_input = self.preprocess_input(state_below, self.context, dropouts)

        last_output, outputs, states = K.rnn(self.step, preprocessed_input,
                                             initial_states,
                                             go_backwards=self.go_backwards,
                                             mask=mask[0],
                                             constants=constants,
                                             unroll=self.unroll,
                                             input_length=state_below.shape[1])
        if self.stateful:
            self.updates = []
            for i in range(len(states)):
                self.updates.append((self.states[i], states[i]))
        if self.return_sequences:
            ret = outputs
        else:
            ret = last_output

        # intermediate states as additional outputs
        if self.return_states:
            ret = [ret, states[0], states[1]]

        return ret

    def compute_mask(self, input, mask):
        if self.return_sequences:
            ret = mask[0]
        else:
            ret = None
        if self.return_states:
            ret = [ret, None, None]
        return ret

    def get_initial_states(self, x):
        # build an all-zero tensor of shape (samples, output_dim)
        if self.init_state is None:
            # build an all-zero tensor of shape (samples, output_dim)
            initial_state = K.zeros_like(x)  # (samples, timesteps, input_dim)
            initial_state = K.sum(initial_state, axis=(1, 2))  # (samples,)
            initial_state = K.expand_dims(initial_state)  # (samples, 1)
            initial_state = K.tile(initial_state, [1, self.output_dim])  # (samples, output_dim)
            if self.init_memory is None:
                initial_states = [initial_state for _ in range(2)]
            else:
                initial_memory = self.init_memory
                initial_states = [initial_state, initial_memory]
        else:
            initial_state = self.init_state
            if self.init_memory is not None: # We have state and memory
                initial_memory = self.init_memory
                initial_states = [initial_state, initial_memory]
            else:
                initial_states = [initial_state for _ in range(2)]

        return initial_states

    def step(self, x, states):

        h_tm1 = states[0]  # State
        c_tm1 = states[1]  # Memory

        B_U = states[2]    # Dropout U
        if self.static_ctx:
            B_W = states[3]    # Dropout W
            context = states[4]
            z = x + K.dot(context * B_W[0], self.W) + K.dot(h_tm1 * B_U[0], self.U) + self.b
        else:
            z = x + K.dot(h_tm1 * B_U[0], self.U) + self.b
        z0 = z[:, :self.output_dim]
        z1 = z[:, self.output_dim: 2 * self.output_dim]
        z2 = z[:, 2 * self.output_dim: 3 * self.output_dim]
        z3 = z[:, 3 * self.output_dim:]

        i = self.inner_activation(z0)
        f = self.inner_activation(z1)
        c = f * c_tm1 + i * self.activation(z2)
        o = self.inner_activation(z3)
        h = o * self.activation(c)
        return h, [h, c]

    def get_constants(self, x):
        constants = []
        dropouts = []
        # States[2]
        if 0 < self.dropout_U < 1:
            ones = K.ones_like(K.reshape(x[:, 0, 0], (-1, 1)))
            ones = K.concatenate([ones] * self.output_dim, 1)
            B_U = [K.in_train_phase(K.dropout(ones, self.dropout_U), ones) for _ in range(4)]
            constants.append(B_U)
        else:
            constants.append([K.cast_to_floatx(1.) for _ in range(4)])

        # States[3]
        if 0 < self.dropout_W < 1:
            input_shape = self.input_spec[1][0].shape
            input_dim = input_shape[-1]
            ones = K.ones_like(K.reshape(x[:, 0, 0], (-1, 1)))
            ones = K.concatenate([ones] * input_dim, 1)
            B_W = [K.in_train_phase(K.dropout(ones, self.dropout_W), ones) for _ in range(4)]
            constants.append(B_W)
        else:
            B_W = [K.cast_to_floatx(1.) for _ in range(4)]
        if self.static_ctx:
            constants.append(B_W)
        else:
            dropouts.append(B_W)

        if 0 < self.dropout_V < 1:
            input_dim = self.input_dim
            ones = K.ones_like(K.reshape(x[:, :, 0], (-1, x.shape[1], 1))) # (bs, timesteps, 1)
            ones = K.concatenate([ones] * input_dim, axis=2)
            B_V = [K.in_train_phase(K.dropout(ones, self.dropout_V), ones) for _ in range(4)]
        else:
            B_V = [K.cast_to_floatx(1.) for _ in range(4)]
        dropouts.append(B_V)
        # States[4]
        if self.static_ctx:
            constants.append(self.context)

        return constants, dropouts

    def get_config(self):
        config = {"output_dim": self.output_dim,
                  "return_states": self.return_states ,
                  "init": self.init.__name__,
                  "inner_init": self.inner_init.__name__,
                  "forget_bias_init": self.forget_bias_init.__name__,
                  "activation": self.activation.__name__,
                  "inner_activation": self.inner_activation.__name__,
                  "W_regularizer": self.W_regularizer.get_config() if self.W_regularizer else None,
                  "V_regularizer": self.V_regularizer.get_config() if self.V_regularizer else None,
                  "U_regularizer": self.U_regularizer.get_config() if self.U_regularizer else None,
                  "b_regularizer": self.b_regularizer.get_config() if self.b_regularizer else None,
                  "dropout_W": self.dropout_W,
                  "dropout_U": self.dropout_U,
                  "dropout_V": self.dropout_V}
        base_config = super(LSTMCond, self).get_config()
        return dict(list(base_config.items()) + list(config.items()))


class LSTMCond2Inputs(Recurrent):
    '''Conditional LSTM: The previously generated word is fed to the current timestep

    # Arguments
        output_dim: dimension of the internal projections and the final output.
        init: weight initialization function.
            Can be the name of an existing function (str),
            or a Theano function (see: [initializations](../initializations.md)).
        inner_init: initialization function of the inner cells.
        return_states: boolean indicating if we want the intermediate states (hidden_state and memory) as additional outputs
        forget_bias_init: initialization function for the bias of the forget gate.
            [Jozefowicz et al.](http://www.jmlr.org/proceedings/papers/v37/jozefowicz15.pdf)
            recommend initializing with ones.
        activation: activation function.
            Can be the name of an existing function (str),
            or a Theano function (see: [activations](../activations.md)).
        inner_activation: activation function for the inner cells.
        W_regularizer: instance of [WeightRegularizer](../regularizers.md)
            (eg. L1 or L2 regularization), applied to the input weights matrices.
        U_regularizer: instance of [WeightRegularizer](../regularizers.md)
            (eg. L1 or L2 regularization), applied to the recurrent weights matrices.
        b_regularizer: instance of [WeightRegularizer](../regularizers.md),
            applied to the bias.
        dropout_W: float between 0 and 1. Fraction of the input units to drop for input gates.
        dropout_U: float between 0 and 1. Fraction of the input units to drop for recurrent connections.

    # References
        - [Long short-term memory](http://deeplearning.cs.cmu.edu/pdfs/Hochreiter97_lstm.pdf) (original 1997 paper)
        - [Learning to forget: Continual prediction with LSTM](http://www.mitpressjournals.org/doi/pdf/10.1162/089976600300015015)
        - [Supervised sequence labelling with recurrent neural networks](http://www.cs.toronto.edu/~graves/preprint.pdf)
        - [A Theoretically Grounded Application of Dropout in Recurrent Neural Networks](http://arxiv.org/abs/1512.05287)
    '''
    def __init__(self, output_dim,
                 init='glorot_uniform', inner_init='orthogonal',
                 return_states=False,
                 forget_bias_init='one', activation='tanh',
                 inner_activation='hard_sigmoid', consume_less='gpu',
                 T_regularizer=None, W_regularizer=None, V_regularizer=None, U_regularizer=None, b_regularizer=None,
                 dropout_T=0., dropout_W=0., dropout_U=0., dropout_V=0., **kwargs):
        self.output_dim = output_dim
        self.init = initializations.get(init)
        self.inner_init = initializations.get(inner_init)
        self.return_states = return_states
        self.forget_bias_init = initializations.get(forget_bias_init)
        self.activation = activations.get(activation)
        self.inner_activation = activations.get(inner_activation)
        self.consume_less = consume_less
        self.dropout_T, self.dropout_W, self.dropout_U, self.dropout_V = dropout_T, dropout_W, dropout_U, dropout_V

        if self.dropout_T or self.dropout_W or self.dropout_U or self.dropout_V:
            self.uses_learning_phase = True

        self.T_regularizer = regularizers.get(T_regularizer)
        self.W_regularizer = regularizers.get(W_regularizer)
        self.V_regularizer = regularizers.get(V_regularizer)
        self.U_regularizer = regularizers.get(U_regularizer)
        self.b_regularizer = regularizers.get(b_regularizer)

        super(LSTMCond2Inputs, self).__init__(**kwargs)

    def build(self, input_shape):
        assert len(input_shape) >= 3 or'You should pass two inputs to LSTMCond ' \
                                       '(previous_embedded_words, context1 and context2) and ' \
                                       'two optional inputs (init_state and init_memory)'

        if len(input_shape) == 3:
            self.input_spec = [InputSpec(shape=input_shape[0]),
                               InputSpec(shape=input_shape[1]),
                               InputSpec(shape=input_shape[2])]
            self.num_inputs = 3
        elif len(input_shape) == 5:
            self.input_spec = [InputSpec(shape=input_shape[0]),
                               InputSpec(shape=input_shape[1]),
                               InputSpec(shape=input_shape[2]),
                               InputSpec(shape=input_shape[3]),
                               InputSpec(shape=input_shape[4])]
            self.num_inputs = 5
        self.input_dim = input_shape[0][2]

        if self.input_spec[1].ndim == 3:
            self.context1_dim = input_shape[1][2]
            self.static_ctx1 = False
            assert input_shape[1][1] == input_shape[0][1], 'When using a 3D ctx in LSTMCond, it has to have the same ' \
                                                          'number of timesteps (dimension 1) as the input. Currently,' \
                                                          'the number of input timesteps is: ' \
                                                           + str(input_shape[0][1]) + \
                                                          ', while the number of ctx timesteps is ' \
                                                           + str(input_shape[1][1]) + ' (complete shapes: '\
                                                           + str(input_shape[0]) + ', ' + str(input_shape[1]) + ')'
        else:
            self.context1_dim = input_shape[1][1]
            self.static_ctx1 = True

        if self.input_spec[2].ndim == 3:
            self.context2_dim = input_shape[2][2]
            self.static_ctx2 = False
            assert input_shape[2][1] == input_shape[0][1], 'When using a 3D ctx in LSTMCond, it has to have the same ' \
                                                          'number of timesteps (dimension 1) as the input. Currently,' \
                                                          'the number of input timesteps is: ' \
                                                           + str(input_shape[0][1]) + \
                                                          ', while the number of ctx timesteps is ' \
                                                           + str(input_shape[2][1]) + ' (complete shapes: '\
                                                           + str(input_shape[0]) + ', ' + str(input_shape[1]) + ')'
        else:
            self.context2_dim = input_shape[2][1]
            self.static_ctx2 = True
        if self.stateful:
            self.reset_states()
        else:
            # initial states: 2 all-zero tensors of shape (output_dim)
            self.states = [None, None] # [h, c]

        if self.consume_less == 'gpu':



            self.T = self.add_weight((self.context1_dim, 4 * self.output_dim),
                                     initializer=self.init,
                                     name='{}_T'.format(self.name),
                                     regularizer=self.W_regularizer)
            self.W = self.add_weight((self.context2_dim, 4 * self.output_dim),
                                     initializer=self.init,
                                     name='{}_W'.format(self.name),
                                     regularizer=self.W_regularizer)
            self.U = self.add_weight((self.output_dim, 4 * self.output_dim),
                                     initializer=self.inner_init,
                                     name='{}_U'.format(self.name),
                                     regularizer=self.U_regularizer)
            self.V = self.add_weight((self.input_dim, 4 * self.output_dim),
                                     initializer=self.init,
                                     name='{}_V'.format(self.name),
                                     regularizer=self.V_regularizer)

            def b_reg(shape, name=None):
                return K.variable(np.hstack((np.zeros(self.output_dim),
                                             K.get_value(self.forget_bias_init((self.output_dim,))),
                                             np.zeros(self.output_dim),
                                             np.zeros(self.output_dim))),
                                  name='{}_b'.format(self.name))
            self.b = self.add_weight((self.output_dim * 4,),
                                     initializer=b_reg,
                                     name='{}_b'.format(self.name),
                                     regularizer=self.b_regularizer)
            self.trainable_weights = [self.T,
                                      self.W,
                                      self.U,
                                      self.V,
                                      self.b]

        else:
            self.T_i = self.add_weight((self.context1_dim, self.output_dim),
                                 initializer=self.init,
                                 name='{}_T_i'.format(self.name),
                                 regularizer=self.W_regularizer)
            self.V_i = self.add_weight((self.input_dim, self.output_dim),
                                 initializer=self.init,
                                 name='{}_V_i'.format(self.name),
                                 regularizer=self.W_regularizer)
            self.W_i = self.add_weight((self.context2_dim, self.output_dim),
                                 initializer=self.init,
                                 name='{}_W_i'.format(self.name),
                                 regularizer=self.W_regularizer)
            self.U_i = self.add_weight((self.output_dim, self.output_dim),
                                       initializer=self.init,
                                       name='{}_U_i'.format(self.name),
                                       regularizer=self.W_regularizer)
            self.b_i = K.zeros((self.output_dim,),
                               initializer='zero',
                               name='{}_b_i'.format(self.name),
                               regularizer=self.b_regularizer)
            self.T_f = self.add_weight((self.context1_dim, self.output_dim),
                                 initializer=self.init,
                                 name='{}_T_f'.format(self.name),
                                 regularizer=self.W_regularizer)
            self.V_f = self.add_weight((self.input_dim, self.output_dim),
                                 initializer=self.init,
                                 name='{}_V_f'.format(self.name),
                                 regularizer=self.W_regularizer)
            self.W_f = self.add_weight((self.context2_dim, self.output_dim),
                                 initializer=self.init,
                                 name='{}_W_f'.format(self.name),
                                 regularizer=self.W_regularizer)
            self.U_f = self.add_weight((self.output_dim, self.output_dim),
                                       initializer=self.init,
                                       name='{}_U_f'.format(self.name),
                                       regularizer=self.W_regularizer)
            self.b_f = self.add_weight((self.output_dim,),
                                             initializer=self.forget_bias_init,
                                             name='{}_b_f'.format(self.name),
                                             regularizer=self.b_regularizer)
            self.T_c = self.add_weight((self.context1_dim, self.output_dim),
                                 initializer=self.init,
                                 name='{}_T_c'.format(self.name),
                                 regularizer=self.W_regularizer)
            self.V_c = self.add_weight((self.input_dim, self.output_dim),
                                 initializer=self.init,
                                 name='{}_V_c'.format(self.name),
                                 regularizer=self.W_regularizer)
            self.W_c = self.add_weight((self.context2_dim, self.output_dim),
                                 initializer=self.init,
                                 name='{}_W_c'.format(self.name),
                                 regularizer=self.W_regularizer)
            self.U_c = self.add_weight((self.output_dim, self.output_dim),
                                       initializer=self.init,
                                       name='{}_U_c'.format(self.name),
                                       regularizer=self.W_regularizer)
            self.b_c = K.zeros((self.output_dim,),
                               initializer='zero',
                               name='{}_b_c'.format(self.name),
                               regularizer=self.b_regularizer)
            self.T_o = self.add_weight((self.context1_dim, self.output_dim),
                                 initializer=self.init,
                                 name='{}_T_o'.format(self.name),
                                 regularizer=self.W_regularizer)
            self.V_o = self.add_weight((self.input_dim, self.output_dim),
                                 initializer=self.init,
                                 name='{}_V_o'.format(self.name),
                                 regularizer=self.W_regularizer)
            self.W_o = self.add_weight((self.context2_dim, self.output_dim),
                                 initializer=self.init,
                                 name='{}_W_o'.format(self.name),
                                 regularizer=self.W_regularizer)
            self.U_o = self.add_weight((self.output_dim, self.output_dim),
                                       initializer=self.init,
                                       name='{}_U_o'.format(self.name),
                                       regularizer=self.W_regularizer)
            self.b_o = K.zeros((self.output_dim,),
                               initializer='zero',
                               name='{}_b_o'.format(self.name),
                               regularizer=self.b_regularizer)
            self.T_x = self.add_weight((self.output_dim, self.context1_dim),
                                 initializer=self.init,
                                 name='{}_T_x'.format(self.name),
                                 regularizer=self.W_regularizer)
            self.V_x = self.add_weight((self.output_dim, self.input_dim),
                                 initializer=self.init,
                                 name='{}_V_x'.format(self.name),
                                 regularizer=self.W_regularizer)
            self.W_x = self.add_weight((self.output_dim, self.context2_dim),
                                 initializer=self.init,
                                 name='{}_W_x'.format(self.name),
                                 regularizer=self.W_regularizer)
            self.b_x = K.zeros((self.output_dim,),
                               initializer='zero',
                               name='{}_b_x'.format(self.name),
                               regularizer=self.b_regularizer)


            self.trainable_weights = [self.T_i, self.V_i, self.W_i, self.U_i, self.b_i,
                                      self.T_c, self.V_c, self.W_c, self.U_c, self.b_c,
                                      self.T_f, self.V_f, self.W_f, self.U_f, self.b_f,
                                      self.T_o, self.V_o, self.W_o, self.U_o, self.b_o,
                                      self.T_x, self.V_x, self.W_x, self.b_x
                                      ]
            self.T = K.concatenate([self.T_i, self.T_f, self.T_c, self.T_o])
            self.W = K.concatenate([self.W_i, self.W_f, self.W_c, self.W_o])
            self.U = K.concatenate([self.U_i, self.U_f, self.U_c, self.U_o])
            self.V = K.concatenate([self.V_i, self.V_f, self.V_c, self.V_o])
            self.b = K.concatenate([self.b_i, self.b_f, self.b_c, self.b_o])


        if self.initial_weights is not None:
            self.set_weights(self.initial_weights)
            del self.initial_weights
        self.built = True

    def reset_states(self):
        assert self.stateful, 'Layer must be stateful.'
        input_shape = self.input_spec[0][0].shape
        if not input_shape[0]:
            raise Exception('If a RNN is stateful, a complete ' +
                            'input_shape must be provided (including batch size).')
        if hasattr(self, 'states'):
            K.set_value(self.states[0],
                        np.zeros((input_shape[0][0], self.output_dim)))
        else:
            self.states = [K.zeros((input_shape[0][0], self.output_dim))]

    def preprocess_input(self, x, context1, context2, dropouts):
        if self.static_ctx1 and self.static_ctx2:
            return K.dot(x * dropouts[0][0], self.V)
        elif self.static_ctx1:
            return K.dot(context2 * dropouts[0][0], self.W) + \
                   K.dot(x * dropouts[1][0], self.V)
        elif self.static_ctx2:
            return K.dot(context1 * dropouts[0][0], self.T) + \
                   K.dot(x * dropouts[1][0], self.V)
        else:
            return K.dot(context1 * dropouts[0][0], self.T) + \
                   K.dot(context2 * dropouts[1][0], self.W) + \
                   K.dot(x * dropouts[2][0], self.V)

    def get_output_shape_for(self, input_shape):
        if self.return_sequences:
            main_out = (input_shape[0][0], input_shape[0][1], self.output_dim)
        else:
            main_out = (input_shape[0][0], self.output_dim)

        if self.return_states:
            states_dim = (input_shape[0][0], input_shape[0][1], self.output_dim)
            main_out = [main_out, states_dim, states_dim]

        return main_out

    def call(self, x, mask=None):
        # input shape: (nb_samples, time (padded with zeros), input_dim)
        # note that the .build() method of subclasses MUST define
        # self.input_spec with a complete input shape.

        input_shape = self.input_spec[0].shape
        state_below = x[0]
        self.context1 = x[1]
        self.context2 = x[2]
        if self.num_inputs == 3: # input: [state_below, context]
            self.init_state = None
            self.init_memory = None
        elif self.num_inputs == 4: # input: [state_below, context, init_generic]
            self.init_state = x[3]
            self.init_memory = x[3]
        elif self.num_inputs == 5: # input: [state_below, context, init_state, init_memory]
            self.init_state = x[3]
            self.init_memory = x[4]
        if K._BACKEND == 'tensorflow':

            if not input_shape[1]:
                raise Exception('When using TensorFlow, you should define '
                                'explicitly the number of timesteps of '
                                'your sequences.\n'
                                'If your first layer is an Embedding, '
                                'make sure to pass it an "input_length" '
                                'argument. Otherwise, make sure '
                                'the first layer has '
                                'an "input_shape" or "batch_input_shape" '
                                'argument, including the time axis. '
                                'Found input shape at layer ' + self.name +
                                ': ' + str(input_shape))
        if self.stateful:
            initial_states = self.states
        else:
            initial_states = self.get_initial_states(state_below)
        constants, dropouts = self.get_constants(state_below)
        preprocessed_input = self.preprocess_input(state_below, self.context1, self.context2, dropouts)

        last_output, outputs, states = K.rnn(self.step, preprocessed_input,
                                             initial_states,
                                             go_backwards=self.go_backwards,
                                             mask=mask[0],
                                             constants=constants,
                                             unroll=self.unroll,
                                             input_length=state_below.shape[1])
        if self.stateful:
            self.updates = []
            for i in range(len(states)):
                self.updates.append((self.states[i], states[i]))
        if self.return_sequences:
            ret = outputs
        else:
            ret = last_output

        # intermediate states as additional outputs
        if self.return_states:
            ret = [ret, states[0], states[1]]

        return ret

    def compute_mask(self, input, mask):
        if self.return_sequences:
            ret = mask[0]
        else:
            ret = None
        if self.return_states:
            ret = [ret, None, None]
        return ret

    def step(self, x, states):

        h_tm1 = states[0]  # State
        c_tm1 = states[1]  # Memory
        B_U = states[2]    # Dropout U

        if self.static_ctx1 and self.static_ctx2:
            B_T = states[3]    # Dropout T
            B_W = states[4]    # Dropout W
            context1 = states[5]
            context2 = states[6]
            z = x + K.dot(context1 * B_T[0], self.T) + K.dot(context2 * B_W[0], self.W) \
                + K.dot(h_tm1 * B_U[0], self.U) + self.b
        elif self.static_ctx1:
            B_T = states[3]    # Dropout T
            context1 = states[4]
            z = x + K.dot(context1 * B_T[0], self.T) + K.dot(h_tm1 * B_U[0], self.U) + self.b
        elif self.static_ctx2:
            B_W = states[3]    # Dropout W
            context2 = states[4]
            z = x + K.dot(context2 * B_W[0], self.W) + K.dot(h_tm1 * B_U[0], self.U) + self.b
        else:
            z = x + K.dot(h_tm1 * B_U[0], self.U) + self.b
        z0 = z[:, :self.output_dim]
        z1 = z[:, self.output_dim: 2 * self.output_dim]
        z2 = z[:, 2 * self.output_dim: 3 * self.output_dim]
        z3 = z[:, 3 * self.output_dim:]

        i = self.inner_activation(z0)
        f = self.inner_activation(z1)
        c = f * c_tm1 + i * self.activation(z2)
        o = self.inner_activation(z3)
        h = o * self.activation(c)
        return h, [h, c]

    def get_constants(self, x):
        constants = []
        dropouts = []
        # States[2]
        if 0 < self.dropout_U < 1:
            ones = K.ones_like(K.reshape(x[:, 0, 0], (-1, 1)))
            ones = K.concatenate([ones] * self.output_dim, 1)
            B_U = [K.in_train_phase(K.dropout(ones, self.dropout_U), ones) for _ in range(4)]
            constants.append(B_U)
        else:
            constants.append([K.cast_to_floatx(1.) for _ in range(4)])

        # States[3]
        if 0 < self.dropout_T < 1:
            input_shape = self.input_spec[1][0].shape
            input_dim = input_shape[-1]
            ones = K.ones_like(K.reshape(x[:, 0, 0], (-1, 1)))
            ones = K.concatenate([ones] * input_dim, 1)
            B_T = [K.in_train_phase(K.dropout(ones, self.dropout_T), ones) for _ in range(4)]
            constants.append(B_T)
        else:
            B_T = [K.cast_to_floatx(1.) for _ in range(4)]
        if self.static_ctx1:
            constants.append(B_T)
        else:
            dropouts.append(B_T)

        # States[4]
        if 0 < self.dropout_W < 1:
            input_shape = self.input_spec[2][0].shape
            input_dim = input_shape[-1]
            ones = K.ones_like(K.reshape(x[:, 0, 0], (-1, 1)))
            ones = K.concatenate([ones] * input_dim, 1)
            B_W = [K.in_train_phase(K.dropout(ones, self.dropout_W), ones) for _ in range(4)]
            constants.append(B_W)
        else:
            B_W = [K.cast_to_floatx(1.) for _ in range(4)]
        if self.static_ctx2:
            constants.append(B_W)
        else:
            dropouts.append(B_W)

        # States[5]
        if 0 < self.dropout_V < 1:
            input_dim = self.input_dim
            ones = K.ones_like(K.reshape(x[:, :, 0], (-1, x.shape[1], 1))) # (bs, timesteps, 1)
            ones = K.concatenate([ones] * input_dim, axis=2)
            B_V = [K.in_train_phase(K.dropout(ones, self.dropout_V), ones) for _ in range(4)]
        else:
            B_V = [K.cast_to_floatx(1.) for _ in range(4)]
        dropouts.append(B_V)
        if self.static_ctx1:
            constants.append(self.context1)
        if self.static_ctx2:
            constants.append(self.context2)

        return constants, dropouts

    def get_initial_states(self, x):
        # build an all-zero tensor of shape (samples, output_dim)
        if self.init_state is None:
            # build an all-zero tensor of shape (samples, output_dim)
            initial_state = K.zeros_like(x)  # (samples, timesteps, input_dim)
            initial_state = K.sum(initial_state, axis=(1, 2))  # (samples,)
            initial_state = K.expand_dims(initial_state)  # (samples, 1)
            initial_state = K.tile(initial_state, [1, self.output_dim])  # (samples, output_dim)
            if self.init_memory is None:
                initial_states = [initial_state for _ in range(2)]
            else:
                initial_memory = self.init_memory
                #reducer = K.ones((self.output_dim, self.output_dim))
                #initial_memory = K.dot(initial_memory, reducer)  # (samples, output_dim)
                initial_states = [initial_state, initial_memory]
        else:
            initial_state = self.init_state
            #reducer = K.ones((self.output_dim, self.output_dim))
            #initial_state = K.dot(initial_state, reducer)  # (samples, output_dim)
            if self.init_memory is not None: # We have state and memory
                initial_memory = self.init_memory
                #reducer = K.ones((self.output_dim, self.output_dim))
                #initial_memory = K.dot(initial_memory, reducer)  # (samples, output_dim)
                initial_states = [initial_state, initial_memory]
            else:
                initial_states = [initial_state for _ in range(2)]

        return initial_states


    def get_config(self):
        config = {"output_dim": self.output_dim,
                  "return_states": self.return_states,
                  "init": self.init.__name__,
                  "inner_init": self.inner_init.__name__,
                  "forget_bias_init": self.forget_bias_init.__name__,
                  "activation": self.activation.__name__,
                  "inner_activation": self.inner_activation.__name__,
                  "T_regularizer": self.T_regularizer.get_config() if self.T_regularizer else None,
                  "W_regularizer": self.W_regularizer.get_config() if self.W_regularizer else None,
                  "V_regularizer": self.V_regularizer.get_config() if self.V_regularizer else None,
                  "U_regularizer": self.U_regularizer.get_config() if self.U_regularizer else None,
                  "b_regularizer": self.b_regularizer.get_config() if self.b_regularizer else None,
                  "dropout_T": self.dropout_T,
                  "dropout_W": self.dropout_W,
                  "dropout_U": self.dropout_U,
                  "dropout_V": self.dropout_V}
        base_config = super(LSTMCond2Inputs, self).get_config()
        return dict(list(base_config.items()) + list(config.items()))


class AttLSTM(Recurrent):
    '''Long-Short Term Memory unit with Attention.

    # Arguments
        output_dim: dimension of the internal projections and the final output.
        output_timesteps: number of output timesteps (# of output vectors generated)
        init: weight initialization function.
            Can be the name of an existing function (str),
            or a Theano function (see: [initializations](../initializations.md)).
        inner_init: initialization function of the inner cells.
        forget_bias_init: initialization function for the bias of the forget gate.
            [Jozefowicz et al.](http://www.jmlr.org/proceedings/papers/v37/jozefowicz15.pdf)
            recommend initializing with ones.
        activation: activation function.
            Can be the name of an existing function (str),
            or a Theano function (see: [activations](../activations.md)).
        inner_activation: activation function for the inner cells.
        W_regularizer: instance of [WeightRegularizer](../regularizers.md)
            (eg. L1 or L2 regularization), applied to the input weights matrices.
        U_regularizer: instance of [WeightRegularizer](../regularizers.md)
            (eg. L1 or L2 regularization), applied to the recurrent weights matrices.
        b_regularizer: instance of [WeightRegularizer](../regularizers.md),
            applied to the bias.
        dropout_W: float between 0 and 1. Fraction of the input units to drop for input gates.
        dropout_U: float between 0 and 1. Fraction of the input units to drop for recurrent connections.
        w_a_regularizer: instance of [WeightRegularizer](../regularizers.md)
            (eg. L1 or L2 regularization), applied to the input weights matrices.
        W_a_regularizer: instance of [WeightRegularizer](../regularizers.md)
            (eg. L1 or L2 regularization), applied to the input weights matrices.
        U_a_regularizer: instance of [WeightRegularizer](../regularizers.md)
            (eg. L1 or L2 regularization), applied to the recurrent weights matrices.
        b_a_regularizer: instance of [WeightRegularizer](../regularizers.md),
            applied to the bias.
        dropout_w_a: float between 0 and 1.
        dropout_W_a: float between 0 and 1.
        dropout_U_a: float between 0 and 1.

    # Formulation

        The resulting attention vector 'phi' at time 't' is formed by applying a weighted sum over
        the set of inputs 'x_i' contained in 'X':

            phi(X, t) = ∑_i alpha_i(t) * x_i,

        where each 'alpha_i' at time 't' is a weighting vector over all the input dimension that
        accomplishes the following condition:

            ∑_i alpha_i = 1

        and is dynamically adapted at each timestep w.r.t. the following formula:

            alpha_i(t) = exp{e_i(t)} /  ∑_j exp{e_j(t)}

        where each 'e_i' at time 't' is calculated as:

            e_i(t) = wa' * tanh( Wa * x_i  +  Ua * h(t-1)  +  ba ),

        where the following are learnable with the respectively named sizes:
                wa                Wa                     Ua                 ba
            [input_dim] [input_dim, input_dim] [output_dim, input_dim] [input_dim]

        The names of 'Ua' and 'Wa' are exchanged w.r.t. the provided reference as well as 'v' being renamed
        to 'x' for matching Keras LSTM's nomenclature.

    # References
        -   Yao L, Torabi A, Cho K, Ballas N, Pal C, Larochelle H, Courville A.
            Describing videos by exploiting temporal structure.
            InProceedings of the IEEE International Conference on Computer Vision 2015 (pp. 4507-4515).
    '''
    def __init__(self, output_dim,
                 init='glorot_uniform', inner_init='orthogonal', init_state=None, init_memory=None,
                 forget_bias_init='one', activation='tanh',
                 inner_activation='hard_sigmoid',
                 W_regularizer=None, U_regularizer=None, b_regularizer=None,
                 dropout_W=0., dropout_U=0., dropout_wa=0., dropout_Wa=0., dropout_Ua=0.,
                 wa_regularizer=None, Wa_regularizer=None, Ua_regularizer=None, ba_regularizer=None,
                 **kwargs):
        self.output_dim = output_dim
        self.init = initializations.get(init)
        self.inner_init = initializations.get(inner_init)
        self.init_state = init_state
        self.init_memory = init_memory
        self.forget_bias_init = initializations.get(forget_bias_init)
        self.activation = activations.get(activation)
        self.inner_activation = activations.get(inner_activation)
        self.W_regularizer = regularizers.get(W_regularizer)
        self.U_regularizer = regularizers.get(U_regularizer)
        self.b_regularizer = regularizers.get(b_regularizer)
        self.dropout_W, self.dropout_U = dropout_W, dropout_U
        # attention model learnable params
        self.wa_regularizer = regularizers.get(wa_regularizer)
        self.Wa_regularizer = regularizers.get(Wa_regularizer)
        self.Ua_regularizer = regularizers.get(Ua_regularizer)
        self.ba_regularizer = regularizers.get(ba_regularizer)
        self.dropout_wa, self.dropout_Wa, self.dropout_Ua = dropout_wa, dropout_Wa, dropout_Ua

        if self.dropout_W or self.dropout_U or self.dropout_wa or self.dropout_Wa or self.dropout_Ua:
            self.uses_learning_phase = True
        super(AttLSTM, self).__init__(**kwargs)
        self.input_spec = [InputSpec(ndim=4)]


    def build(self, input_shape):
        self.input_spec = [InputSpec(shape=input_shape, ndim=4)]
        self.input_dim = input_shape[-1]

        if self.stateful:
            self.reset_states()
        else:
            # initial states: 2 all-zero tensors of shape (output_dim)
            self.states = [None, None]

        # Initialize Att model params (following the same format for any option of self.consume_less)
        self.wa = self.add_weight((self.input_dim,),
                                   initializer=self.init,
                                   name='{}_wa'.format(self.name),
                                   regularizer=self.wa_regularizer)

        self.Wa = self.add_weight((self.input_dim, self.input_dim),
                                   initializer=self.init,
                                   name='{}_Wa'.format(self.name),
                                   regularizer=self.Wa_regularizer)
        self.Ua = self.add_weight((self.output_dim, self.input_dim),
                                   initializer=self.inner_init,
                                   name='{}_Ua'.format(self.name),
                                   regularizer=self.Ua_regularizer)

        self.ba = self.add_weight(self.input_dim,
                                  initializer=K.variable(np.zeros(self.input_dim),
                                                          name='{}_ba'.format(self.name)),
                                  regularizer=self.ba_regularizer)

        if self.consume_less == 'gpu':
            self.W = self.add_weight((self.input_dim, 4 * self.output_dim),
                                     initializer=self.init,
                                     name='{}_W'.format(self.name),
                                     regularizer=self.W_regularizer)
            self.U = self.add_weight((self.output_dim, 4 * self.output_dim),
                                     initializer=self.inner_init,
                                     name='{}_U'.format(self.name),
                                     regularizer=self.U_regularizer)

            def b_reg(shape, name=None):
                return K.variable(np.hstack((np.zeros(self.output_dim),
                                             K.get_value(self.forget_bias_init((self.output_dim,))),
                                             np.zeros(self.output_dim),
                                             np.zeros(self.output_dim))),
                                  name='{}_b'.format(self.name))
            self.b = self.add_weight((self.output_dim * 4,),
                                     initializer=b_reg,
                                     name='{}_b'.format(self.name),
                                     regularizer=self.b_regularizer)

            self.trainable_weights = [self.wa, self.Wa, self.Ua, self.ba, # AttModel parameters
                                      self.W, self.U, self.b]
        else:
            self.W_i = self.add_weight((self.input_dim, self.output_dim),
                                       initializer=self.init,
                                       name='{}_W_i'.format(self.name),
                                       regularizer=self.W_regularizer)
            self.U_i = self.add_weight((self.output_dim, self.output_dim),
                                       initializer=self.init,
                                       name='{}_U_i'.format(self.name),
                                       regularizer=self.W_regularizer)
            self.b_i = self.add_weight((self.output_dim,),
                                       initializer='zero',
                                       name='{}_b_i'.format(self.name),
                                       regularizer=self.b_regularizer)
            self.W_f = self.add_weight((self.input_dim, self.output_dim),
                                       initializer=self.init,
                                       name='{}_W_f'.format(self.name),
                                       regularizer=self.W_regularizer)
            self.U_f = self.add_weight((self.output_dim, self.output_dim),
                                       initializer=self.init,
                                       name='{}_U_f'.format(self.name),
                                       regularizer=self.W_regularizer)
            self.b_f = self.add_weight((self.output_dim,),
                                       initializer=self.forget_bias_init,
                                       name='{}_b_f'.format(self.name),
                                       regularizer=self.b_regularizer)
            self.W_c = self.add_weight((self.input_dim, self.output_dim),
                                       initializer=self.init,
                                       name='{}_W_c'.format(self.name),
                                       regularizer=self.W_regularizer)
            self.U_c = self.add_weight((self.output_dim, self.output_dim),
                                       initializer=self.init,
                                       name='{}_U_c'.format(self.name),
                                       regularizer=self.W_regularizer)
            self.b_c = self.add_weight((self.output_dim,),
                                       initializer='zero',
                                       name='{}_b_c'.format(self.name),
                                       regularizer=self.b_regularizer)
            self.W_o = self.add_weight((self.input_dim, self.output_dim),
                                       initializer=self.init,
                                       name='{}_W_o'.format(self.name),
                                       regularizer=self.W_regularizer)
            self.U_o = self.add_weight((self.output_dim, self.output_dim),
                                       initializer=self.init,
                                       name='{}_U_o'.format(self.name),
                                       regularizer=self.W_regularizer)
            self.b_o = self.add_weight((self.output_dim,),
                                       initializer='zero',
                                       name='{}_b_o'.format(self.name),
                                       regularizer=self.b_regularizer)

            self.trainable_weights = [self.wa, self.Wa, self.Ua, self.ba, # AttModel parameters
                                      self.W_i, self.U_i, self.b_i,
                                      self.W_c, self.U_c, self.b_c,
                                      self.W_f, self.U_f, self.b_f,
                                      self.W_o, self.U_o, self.b_o]

            self.W = K.concatenate([self.W_i, self.W_f, self.W_c, self.W_o])
            self.U = K.concatenate([self.U_i, self.U_f, self.U_c, self.U_o])
            self.b = K.concatenate([self.b_i, self.b_f, self.b_c, self.b_o])

        if self.initial_weights is not None:
            self.set_weights(self.initial_weights)
            del self.initial_weights
        self.built = True

    def reset_states(self):
        assert self.stateful, 'Layer must be stateful.'
        input_shape = self.input_spec[0].shape
        if not input_shape[0]:
            raise ValueError('If a RNN is stateful, a complete ' +
                             'input_shape must be provided (including batch size).')
        if hasattr(self, 'states'):
            K.set_value(self.states[0],
                        np.zeros((input_shape[0], self.output_dim)))
            K.set_value(self.states[1],
                        np.zeros((input_shape[0], self.output_dim)))
        else:
            self.states = [K.zeros((input_shape[0], self.output_dim)),
                           K.zeros((input_shape[0], self.output_dim))]

    def preprocess_input(self, x):
        return x

    def call(self, x, mask=None):
        # input shape: (nb_samples, time (padded with zeros), input_dim)
        # note that the .build() method of subclasses MUST define
        # self.input_spec with a complete input shape.
        input_shape = self.input_spec[0].shape
        if K._BACKEND == 'tensorflow':
            if not input_shape[1]:
                raise Exception('When using TensorFlow, you should define '
                                'explicitly the number of timesteps of '
                                'your sequences.\n'
                                'If your first layer is an Embedding, '
                                'make sure to pass it an "input_length" '
                                'argument. Otherwise, make sure '
                                'the first layer has '
                                'an "input_shape" or "batch_input_shape" '
                                'argument, including the time axis. '
                                'Found input shape at layer ' + self.name +
                                ': ' + str(input_shape))
        if self.stateful:
            initial_states = self.states
        else:
            initial_states = self.get_initial_states(x)
        constants = self.get_constants(x)
        preprocessed_input = self.preprocess_input(x)

        last_output, outputs, states = K.rnn(self.step, preprocessed_input,
                                             initial_states,
                                             go_backwards=self.go_backwards,
                                             mask=None,
                                             constants=constants,
                                             unroll=self.unroll,
                                             input_length=input_shape[1])
        if self.stateful:
            self.updates = []
            for i in range(len(states)):
                self.updates.append((self.states[i], states[i]))

        if self.return_sequences:
            return outputs
        else:
            return last_output

    def step(self, x, states):
        # After applying a RepeatMatrix before this AttLSTM the following way:
        #    x = RepeatMatrix(out_timesteps, dim=1)(x)
        #    x will have the following size:
        #        [batch_size, out_timesteps, in_timesteps, dim_encoder]
        #    which means that in step() our x will be:
        #        [batch_size, in_timesteps, dim_encoder]
        h_tm1 = states[0]
        c_tm1 = states[1]
        B_U = states[2]
        B_W = states[3]
        # Att model dropouts
        B_wa = states[4]
        context = states[5] # pre-calculated Wa*x term (common for all output timesteps)
        B_Ua = states[6]

        # AttModel (see Formulation in class header)
        e = K.dot(K.tanh(context + K.dot(h_tm1[:, None, :] * B_Ua, self.Ua) + self.ba) * B_wa, self.wa)
        alpha = K.softmax(e)
        x_ = (x * alpha[:,:,None]).sum(axis=1) # sum over the in_timesteps dimension resulting in [batch_size, input_dim]

        # LSTM
        if self.consume_less == 'gpu':
            z = K.dot(x_ * B_W[0], self.W) + K.dot(h_tm1 * B_U[0], self.U) + self.b

            z0 = z[:, :self.output_dim]
            z1 = z[:, self.output_dim: 2 * self.output_dim]
            z2 = z[:, 2 * self.output_dim: 3 * self.output_dim]
            z3 = z[:, 3 * self.output_dim:]

            i = self.inner_activation(z0)
            f = self.inner_activation(z1)
            c = f * c_tm1 + i * self.activation(z2)
            o = self.inner_activation(z3)

        else:
            if self.consume_less == 'cpu':
                x_i = x_[:, :self.output_dim]
                x_f = x_[:, self.output_dim: 2 * self.output_dim]
                x_c = x_[:, 2 * self.output_dim: 3 * self.output_dim]
                x_o = x_[:, 3 * self.output_dim:]
            elif self.consume_less == 'mem':
                x_i = K.dot(x_ * B_W[0], self.W_i) + self.b_i
                x_f = K.dot(x_ * B_W[1], self.W_f) + self.b_f
                x_c = K.dot(x_ * B_W[2], self.W_c) + self.b_c
                x_o = K.dot(x_ * B_W[3], self.W_o) + self.b_o
            else:
                raise Exception('Unknown `consume_less` mode.')

            i = self.inner_activation(x_i + K.dot(h_tm1 * B_U[0], self.U_i))
            f = self.inner_activation(x_f + K.dot(h_tm1 * B_U[1], self.U_f))
            c = f * c_tm1 + i * self.activation(x_c + K.dot(h_tm1 * B_U[2], self.U_c))
            o = self.inner_activation(x_o + K.dot(h_tm1 * B_U[3], self.U_o))

        h = o * self.activation(c)
        return h, [h, c]

    def get_constants(self, x):
        constants = []
        if 0 < self.dropout_U < 1:
            ones = K.ones_like(K.reshape(x[:, 0, 0, 0], (-1, 1)))
            ones = K.concatenate([ones] * self.output_dim, 1)
            B_U = [K.in_train_phase(K.dropout(ones, self.dropout_U), ones) for _ in range(4)]
            constants.append(B_U)
        else:
            constants.append([K.cast_to_floatx(1.) for _ in range(4)])

        if 0 < self.dropout_W < 1:
            input_shape = self.input_spec[0].shape
            input_dim = input_shape[-1]
            ones = K.ones_like(K.reshape(x[:, 0, 0, 0], (-1, 1)))
            ones = K.concatenate([ones] * input_dim, 1)
            B_W = [K.in_train_phase(K.dropout(ones, self.dropout_W), ones) for _ in range(4)]
            constants.append(B_W)
        else:
            constants.append([K.cast_to_floatx(1.) for _ in range(4)])

        # AttModel
        if 0 < self.dropout_wa < 1:
            input_shape = self.input_spec[0].shape
            input_dim = input_shape[-1]
            ones = K.ones_like(K.reshape(x[:, :, 0, 0], (-1, input_shape[1], 1)))
            ones = K.concatenate([ones] * input_dim, 2)
            B_wa = K.in_train_phase(K.dropout(ones, self.dropout_wa), ones)
            constants.append(B_wa)
        else:
            constants.append(K.cast_to_floatx(1.))

        if 0 < self.dropout_Wa < 1:
            input_shape = self.input_spec[0].shape
            input_dim = input_shape[-1]
            ones = K.ones_like(K.reshape(x[:, :, 0, 0], (-1, input_shape[1], 1)))
            ones = K.concatenate([ones] * input_dim, 2)
            B_Wa = K.in_train_phase(K.dropout(ones, self.dropout_Wa), ones)
            constants.append(K.dot(x[:, 0, :, :] * B_Wa, self.Wa))
        else:
            constants.append(K.dot(x[:, 0, :, :], self.Wa))

        if 0 < self.dropout_Ua < 1:
            input_shape = self.input_spec[0].shape
            ones = K.ones_like(K.reshape(x[:, :, 0, 0], (-1, input_shape[1], 1)))
            ones = K.concatenate([ones] * self.output_dim, 2)
            B_Ua = K.in_train_phase(K.dropout(ones, self.dropout_Ua), ones)
            constants.append(B_Ua)
        else:
            constants.append([K.cast_to_floatx(1.)])

        return constants

    def get_config(self):
        config = {'output_dim': self.output_dim,
                  'init': self.init.__name__,
                  'inner_init': self.inner_init.__name__,
                  'forget_bias_init': self.forget_bias_init.__name__,
                  'activation': self.activation.__name__,
                  'inner_activation': self.inner_activation.__name__,
                  'W_regularizer': self.W_regularizer.get_config() if self.W_regularizer else None,
                  'U_regularizer': self.U_regularizer.get_config() if self.U_regularizer else None,
                  'b_regularizer': self.b_regularizer.get_config() if self.b_regularizer else None,
                  'wa_regularizer': self.wa_regularizer.get_config() if self.wa_regularizer else None,
                  'Wa_regularizer': self.Wa_regularizer.get_config() if self.Wa_regularizer else None,
                  'Ua_regularizer': self.Ua_regularizer.get_config() if self.Ua_regularizer else None,
                  'ba_regularizer': self.ba_regularizer.get_config() if self.ba_regularizer else None,
                  'dropout_W': self.dropout_W,
                  'dropout_U': self.dropout_U,
                  'dropout_wa': self.dropout_wa,
                  'dropout_Wa': self.dropout_Wa,
                  'dropout_Ua': self.dropout_Ua}
        base_config = super(AttLSTM, self).get_config()
        return dict(list(base_config.items()) + list(config.items()))


class AttLSTMCond(Recurrent):
    '''Long-Short Term Memory unit with Attention + the previously generated word fed to the current timestep.
    You should give two inputs to this layer:
        1. The shifted sequence of words (shape: (mini_batch_size, output_timesteps, embedding_size))
        2. The complete input sequence (shape: (mini_batch_size, input_timesteps, input_dim))
    # Arguments
        output_dim: dimension of the internal projections and the final output.
        embedding_size: dimension of the word embedding module used for the enconding of the generated words.
        return_extra_variables: indicates if we only need the LSTM hidden state (False) or we want 
            additional internal variables as outputs (True). The additional variables provided are:
            - x_att (None, out_timesteps, dim_encoder): feature vector computed after the Att.Model at each timestep
            - alphas (None, out_timesteps, in_timesteps): weights computed by the Att.Model at each timestep
        return_states: boolean indicating if we want the intermediate states (hidden_state and memory) as additional outputs
        output_timesteps: number of output timesteps (# of output vectors generated)
        init: weight initialization function.
            Can be the name of an existing function (str),
            or a Theano function (see: [initializations](../initializations.md)).
        inner_init: initialization function of the inner cells.
        forget_bias_init: initialization function for the bias of the forget gate.
            [Jozefowicz et al.](http://www.jmlr.org/proceedings/papers/v37/jozefowicz15.pdf)
            recommend initializing with ones.
        activation: activation function.
            Can be the name of an existing function (str),
            or a Theano function (see: [activations](../activations.md)).
        inner_activation: activation function for the inner cells.
        W_regularizer: instance of [WeightRegularizer](../regularizers.md)
            (eg. L1 or L2 regularization), applied to the input weights matrices.
        U_regularizer: instance of [WeightRegularizer](../regularizers.md)
            (eg. L1 or L2 regularization), applied to the recurrent weights matrices.
        b_regularizer: instance of [WeightRegularizer](../regularizers.md),
            applied to the bias.
        dropout_W: float between 0 and 1. Fraction of the input units to drop for input gates.
        dropout_U: float between 0 and 1. Fraction of the input units to drop for recurrent connections.
        w_a_regularizer: instance of [WeightRegularizer](../regularizers.md)
            (eg. L1 or L2 regularization), applied to the input weights matrices.
        W_a_regularizer: instance of [WeightRegularizer](../regularizers.md)
            (eg. L1 or L2 regularization), applied to the input weights matrices.
        U_a_regularizer: instance of [WeightRegularizer](../regularizers.md)
            (eg. L1 or L2 regularization), applied to the recurrent weights matrices.
        b_a_regularizer: instance of [WeightRegularizer](../regularizers.md),
            applied to the bias.
        dropout_w_a: float between 0 and 1.
        dropout_W_a: float between 0 and 1.
        dropout_U_a: float between 0 and 1.

    # Formulation

        The resulting attention vector 'phi' at time 't' is formed by applying a weighted sum over
        the set of inputs 'x_i' contained in 'X':

            phi(X, t) = ∑_i alpha_i(t) * x_i,

        where each 'alpha_i' at time 't' is a weighting vector over all the input dimension that
        accomplishes the following condition:

            ∑_i alpha_i = 1

        and is dynamically adapted at each timestep w.r.t. the following formula:

            alpha_i(t) = exp{e_i(t)} /  ∑_j exp{e_j(t)}

        where each 'e_i' at time 't' is calculated as:

            e_i(t) = wa' * tanh( Wa * x_i  +  Ua * h(t-1)  +  ba ),

        where the following are learnable with the respectively named sizes:
                wa                Wa                     Ua                 ba
            [input_dim] [input_dim, input_dim] [output_dim, input_dim] [input_dim]

        The names of 'Ua' and 'Wa' are exchanged w.r.t. the provided reference as well as 'v' being renamed
        to 'x' for matching Keras LSTM's nomenclature.

    # References
        -   Yao L, Torabi A, Cho K, Ballas N, Pal C, Larochelle H, Courville A.
            Describing videos by exploiting temporal structure.
            InProceedings of the IEEE International Conference on Computer Vision 2015 (pp. 4507-4515).
    '''
    def __init__(self, output_dim, return_extra_variables=False, return_states=False,
                 init='glorot_uniform', inner_init='orthogonal', init_att='glorot_uniform',
                 forget_bias_init='one', activation='tanh', inner_activation='sigmoid', mask_value=0.,
                 W_regularizer=None, U_regularizer=None, V_regularizer=None, b_regularizer=None,
                 wa_regularizer=None, Wa_regularizer=None, Ua_regularizer=None, ba_regularizer=None, ca_regularizer=None,
                 dropout_W=0., dropout_U=0., dropout_V=0., dropout_wa=0., dropout_Wa=0., dropout_Ua=0.,
                 **kwargs):
        self.output_dim = output_dim
        self.return_extra_variables = return_extra_variables
        self.return_states = return_states
        self.init = initializations.get(init)
        self.inner_init = initializations.get(inner_init)
        self.init_att = initializations.get(init_att)
        self.forget_bias_init = initializations.get(forget_bias_init)
        self.activation = activations.get(activation)
        self.inner_activation = activations.get(inner_activation)
        self.mask_value = mask_value
        # Regularizers
        self.W_regularizer = regularizers.get(W_regularizer)
        self.U_regularizer = regularizers.get(U_regularizer)
        self.V_regularizer = regularizers.get(V_regularizer)
        self.b_regularizer = regularizers.get(b_regularizer)
        # attention model learnable params
        self.wa_regularizer = regularizers.get(wa_regularizer)
        self.Wa_regularizer = regularizers.get(Wa_regularizer)
        self.Ua_regularizer = regularizers.get(Ua_regularizer)
        self.ba_regularizer = regularizers.get(ba_regularizer)
        self.ca_regularizer = regularizers.get(ca_regularizer)

        # Dropouts
        self.dropout_W, self.dropout_U, self.dropout_V = dropout_W, dropout_U, dropout_V
        self.dropout_wa, self.dropout_Wa, self.dropout_Ua = dropout_wa, dropout_Wa, dropout_Ua

        if self.dropout_W or self.dropout_U or self.dropout_wa or self.dropout_Wa or self.dropout_Ua:
            self.uses_learning_phase = True
        super(AttLSTMCond, self).__init__(**kwargs)


    def build(self, input_shape):
        assert len(input_shape) == 2 or len(input_shape) == 4, 'You should pass two inputs to AttLSTMCond ' \
                                                               '(previous_embedded_words and context) ' \
                                                               'and two optional inputs (init_state and init_memory)'

        if len(input_shape) == 2:
            self.input_spec = [InputSpec(shape=input_shape[0]), InputSpec(shape=input_shape[1])]
            self.num_inputs = 2
        elif len(input_shape) == 4:
            self.input_spec = [InputSpec(shape=input_shape[0]), InputSpec(shape=input_shape[1]),
                               InputSpec(shape=input_shape[2]), InputSpec(shape=input_shape[3])]
            self.num_inputs = 4
        self.input_dim = input_shape[0][2]
        self.context_steps = input_shape[1][1]
        self.context_dim = input_shape[1][2]
        if self.stateful:
            self.reset_states()
        else:
            # initial states: all-zero tensors of shape (output_dim)
            self.states = [None, None, None] # [h, c, x_att]

        # Initialize Att model params (following the same format for any option of self.consume_less)
        self.wa = self.add_weight((self.context_dim, ),
                                   initializer=self.init_att,
                                   name='{}_wa'.format(self.name),
                                   regularizer=self.wa_regularizer)

        self.Wa = self.add_weight((self.output_dim, self.context_dim),
                                   initializer=self.init_att,
                                   name='{}_Wa'.format(self.name),
                                   regularizer=self.Wa_regularizer)
        self.Ua = self.add_weight((self.context_dim, self.context_dim),
                                   initializer=self.inner_init,
                                   name='{}_Ua'.format(self.name),
                                   regularizer=self.Ua_regularizer)

        self.ba = self.add_weight(self.context_dim,
                                   initializer='zero',
                                   name='{}_ba'.format(self.name),
                                  regularizer=self.ba_regularizer)

        self.ca = self.add_weight(self.context_steps,
                                  initializer='zero',
                                   name='{}_ca'.format(self.name),
                                  regularizer=self.ca_regularizer)

        if self.consume_less == 'gpu':
            self.W = self.add_weight((self.context_dim, 4 * self.output_dim),
                                     initializer=self.init,
                                     name='{}_W'.format(self.name),
                                     regularizer=self.W_regularizer)
            self.U = self.add_weight((self.output_dim, 4 * self.output_dim),
                                     initializer=self.inner_init,
                                     name='{}_U'.format(self.name),
                                     regularizer=self.U_regularizer)
            self.V = self.add_weight((self.input_dim, 4 * self.output_dim),
                                      initializer=self.init,
                                     name='{}_V'.format(self.name),
                                     regularizer=self.V_regularizer)
            def b_reg(shape, name=None):
                return K.variable(np.hstack((np.zeros(self.output_dim),
                                             K.get_value(self.forget_bias_init((self.output_dim,))),
                                             np.zeros(self.output_dim),
                                             np.zeros(self.output_dim))),
                                  name='{}_b'.format(self.name))

            self.b = self.add_weight((self.output_dim * 4,),
                                     initializer=b_reg,
                                     name='{}_b'.format(self.name),
                                     regularizer=self.b_regularizer)

            self.trainable_weights = [self.wa, self.Wa, self.Ua, self.ba, self.ca,  # AttModel parameters
                                      self.V, # LSTMCond weights
                                      self.W, self.U, self.b]
        else:
            self.V_i = self.add_weight((self.input_dim, self.output_dim),
                                 initializer=self.init,
                                 name='{}_V_i'.format(self.name),
                                 regularizer=self.W_regularizer)
            self.W_i = self.add_weight((self.context_dim, self.output_dim),
                                 initializer=self.init,
                                 name='{}_W_i'.format(self.name),
                                 regularizer=self.W_regularizer)
            self.U_i = self.add_weight((self.output_dim, self.output_dim),
                                       initializer=self.inner_init,
                                       name='{}_U_i'.format(self.name),
                                       regularizer=self.W_regularizer)
            self.b_i = self.add_weight((self.output_dim,),
                                       initializer='zero',
                                       name='{}_b_i'.format(self.name),
                                       regularizer=self.b_regularizer)
            self.V_f = self.add_weight((self.input_dim, self.output_dim),
                                 initializer=self.init,
                                 name='{}_V_f'.format(self.name),
                                 regularizer=self.W_regularizer)
            self.W_f = self.add_weight((self.context_dim, self.output_dim),
                                 initializer=self.init,
                                 name='{}_W_f'.format(self.name),
                                 regularizer=self.W_regularizer)
            self.U_f = self.add_weight((self.output_dim, self.output_dim),
                                       initializer=self.inner_init,
                                       name='{}_U_f'.format(self.name),
                                       regularizer=self.W_regularizer)
            self.b_f = self.add_weight((self.output_dim,),
                                       initializer=self.forget_bias_init,
                                       name='{}_b_f'.format(self.name),
                                       regularizer=self.b_regularizer)
            self.V_c = self.add_weight((self.input_dim, self.output_dim),
                                 initializer=self.init,
                                 name='{}_V_c'.format(self.name),
                                 regularizer=self.W_regularizer)
            self.W_c = self.add_weight((self.context_dim, self.output_dim),
                                 initializer=self.init,
                                 name='{}_W_c'.format(self.name),
                                 regularizer=self.W_regularizer)
            self.U_c = self.add_weight((self.output_dim, self.output_dim),
                                       initializer=self.inner_init,
                                       name='{}_U_c'.format(self.name),
                                       regularizer=self.W_regularizer)
            self.b_c = self.add_weight((self.output_dim,),
                                       initializer='zero',
                                       name='{}_b_c'.format(self.name),
                                       regularizer=self.b_regularizer)
            self.V_o = self.add_weight((self.input_dim, self.output_dim),
                                 initializer=self.init,
                                 name='{}_V_o'.format(self.name),
                                 regularizer=self.W_regularizer)
            self.W_o = self.add_weight((self.context_dim, self.output_dim),
                                 initializer='zero',
                                 name='{}_W_o'.format(self.name),
                                 regularizer=self.W_regularizer)
            self.U_o = self.add_weight((self.output_dim, self.output_dim),
                                       initializer=self.inner_init,
                                       name='{}_U_o'.format(self.name),
                                       regularizer=self.W_regularizer)
            self.b_o = self.add_weight((self.output_dim,),
                                       initializer='zero',
                                       name='{}_b_o'.format(self.name),
                                       regularizer=self.b_regularizer)
            self.V_x = self.add_weight((self.output_dim, self.input_dim),
                                 initializer=self.init,
                                 name='{}_V_x'.format(self.name),
                                 regularizer=self.W_regularizer)
            self.W_x = self.add_weight((self.output_dim, self.context_dim),
                                 initializer=self.init,
                                 name='{}_W_x'.format(self.name),
                                 regularizer=self.W_regularizer)
            self.b_x = self.add_weight((self.output_dim,),
                                       initializer='zero',
                                       name='{}_b_x'.format(self.name),
                                       regularizer=self.b_regularizer)

            self.trainable_weights = [self.wa, self.Wa, self.Ua, self.ba, self.ca, # AttModel parameters
                                      self.V_i, self.W_i, self.U_i, self.b_i,
                                      self.V_c, self.W_c, self.U_c, self.b_c,
                                      self.V_f, self.W_f, self.U_f, self.b_f,
                                      self.V_o, self.W_o, self.U_o, self.b_o,
                                      self.V_x, self.W_x, self.b_x
                                      ]

            self.W = K.concatenate([self.W_i, self.W_f, self.W_c, self.W_o])
            self.U = K.concatenate([self.U_i, self.U_f, self.U_c, self.U_o])
            self.V = K.concatenate([self.V_i, self.V_f, self.V_c, self.V_o])
            self.b = K.concatenate([self.b_i, self.b_f, self.b_c, self.b_o])


        if self.initial_weights is not None:
            self.set_weights(self.initial_weights)
            del self.initial_weights
        self.built = True

    def reset_states(self):
        assert self.stateful, 'Layer must be stateful.'
        input_shape = self.input_shape
        if not input_shape[0]:
            raise Exception('If a RNN is stateful, a complete ' +
                            'input_shape must be provided (including batch size).')
        if hasattr(self, 'states'):
            K.set_value(self.states[0],
                        np.zeros((input_shape[0], self.output_dim)))
            K.set_value(self.states[1],
                        np.zeros((input_shape[0], self.output_dim)))
            K.set_value(self.states[2],
                        np.zeros((input_shape[0], input_shape[3])))
        else:
            self.states = [K.zeros((input_shape[0], self.output_dim)),
                           K.zeros((input_shape[0], self.output_dim)),
                           K.zeros((input_shape[0], input_shape[3]))]

    def preprocess_input(self, x, B_V):
        return K.dot(x * B_V[0], self.V)

    def get_output_shape_for(self, input_shape):
        if self.return_sequences:
            main_out = (input_shape[0][0], input_shape[0][1], self.output_dim)
        else:
            main_out = (input_shape[0][0], self.output_dim)

        if self.return_extra_variables:
            dim_x_att = (input_shape[0][0], input_shape[0][1], self.context_dim)
            dim_alpha_att = (input_shape[0][0], input_shape[0][1], input_shape[1][1])
            main_out = [main_out, dim_x_att, dim_alpha_att]

        if self.return_states:
            if not isinstance(main_out, list):
                main_out = [main_out]
            states_dim = (input_shape[0][0], input_shape[0][1], self.output_dim)
            main_out += [states_dim, states_dim]

        return main_out

    def call(self, x, mask=None):
        # input shape: (nb_samples, time (padded with zeros), input_dim)
        # note that the .build() method of subclasses MUST define
        # self.input_spec with a complete input shape.

        input_shape = self.input_spec[0].shape
        state_below = x[0]
        self.context = x[1]
        if self.num_inputs == 2: # input: [state_below, context]
            self.init_state = None
            self.init_memory = None
        elif self.num_inputs == 3: # input: [state_below, context, init_generic]
            self.init_state = x[2]
            self.init_memory = x[2]
        elif self.num_inputs == 4: # input: [state_below, context, init_state, init_memory]
            self.init_state = x[2]
            self.init_memory = x[3]
        if K._BACKEND == 'tensorflow':
            if not input_shape[1]:
                raise Exception('When using TensorFlow, you should define '
                                'explicitly the number of timesteps of '
                                'your sequences.\n'
                                'If your first layer is an Embedding, '
                                'make sure to pass it an "input_length" '
                                'argument. Otherwise, make sure '
                                'the first layer has '
                                'an "input_shape" or "batch_input_shape" '
                                'argument, including the time axis. '
                                'Found input shape at layer ' + self.name +
                                ': ' + str(input_shape))
        if self.stateful:
            initial_states = self.states
        else:
            initial_states = self.get_initial_states(state_below)

        constants, B_V = self.get_constants(state_below, mask[1])
        preprocessed_input = self.preprocess_input(state_below, B_V)
        last_output, outputs, states = K.rnn(self.step,
                                             preprocessed_input,
                                             initial_states,
                                             go_backwards=self.go_backwards,
                                             mask=mask[0],
                                             constants=constants,
                                             unroll=self.unroll,
                                             input_length=state_below.shape[1],
                                             pos_extra_outputs_states=[2, 3])
        if self.stateful:
            self.updates = []
            for i in range(len(states)):
                self.updates.append((self.states[i], states[i]))

        if self.return_sequences:
            ret = outputs
        else:
            ret = last_output

        if self.return_extra_variables:
            ret = [ret, states[2], states[3]]

        # intermediate states as additional outputs
        if self.return_states:
            if not isinstance(ret, list):
                ret = [ret]
            ret += [states[0], states[1]]

        return ret

    def compute_mask(self, input, mask):
        if self.return_extra_variables:
            ret = [mask[0], mask[0], mask[0]]
        else:
            ret = mask[0]

        if self.return_states:
            if not isinstance(ret, list):
                ret = [ret]
            ret += [mask[0], mask[0]]

        return ret

    def step(self, x, states):
        h_tm1 = states[0]                                 # State
        c_tm1 = states[1]                                 # Memory
        non_used_x_att = states[2]                        # Placeholder for returning extra variables
        non_used_alphas_att = states[3]                   # Placeholder for returning extra variables
        B_U = states[4]                                   # Dropout U
        B_W = states[5]                                   # Dropout W
        # Att model dropouts
        B_wa = states[6]                                  # Dropout wa
        B_Wa = states[7]                                  # Dropout Wa
        pctx_ = states[8]                                 # Projected context (i.e. context * Ua + ba)
        context = states[9]                               # Original context
        mask_context = states[10]                         # Context mask
        if mask_context.ndim > 1:                         # Mask the context (only if necessary)
            pctx_ = mask_context[:, :, None] * pctx_
            context = mask_context[:, :, None] * context

        # Attention model (see Formulation in class header)
        p_state_ = K.dot(h_tm1 * B_Wa[0], self.Wa)
        pctx_ = K.tanh(pctx_ + p_state_[:, None, :])
        e = K.dot(pctx_ * B_wa[0], self.wa) + self.ca
        if mask_context.ndim > 1: # Mask the context (only if necessary)
            e = mask_context * e
        alphas_shape = e.shape
        alphas = K.softmax(e.reshape([alphas_shape[0], alphas_shape[1]]))
        # sum over the in_timesteps dimension resulting in [batch_size, input_dim]
        ctx_ = (context * alphas[:, :, None]).sum(axis=1)
        # LSTM
        if self.consume_less == 'gpu':
            z = x + \
                K.dot(h_tm1 * B_U[0], self.U)  + \
                K.dot(ctx_ * B_W[0], self.W) + \
                self.b

            z0 = z[:, :self.output_dim]
            z1 = z[:, self.output_dim: 2 * self.output_dim]
            z2 = z[:, 2 * self.output_dim: 3 * self.output_dim]
            z3 = z[:, 3 * self.output_dim:]
            i = self.inner_activation(z0)
            f = self.inner_activation(z1)
            o = self.inner_activation(z3)
            c = f * c_tm1 + i * self.activation(z2)
        h = o * self.activation(c)

        return h, [h, c, ctx_, alphas]

    def get_constants(self, x, mask_context):
        constants = []
        # States[4]
        if 0 < self.dropout_U < 1:
            ones = K.ones_like(K.reshape(x[:, 0, 0], (-1, 1)))
            ones = K.concatenate([ones] * self.output_dim, 1)
            B_U = [K.in_train_phase(K.dropout(ones, self.dropout_U), ones) for _ in range(4)]
            constants.append(B_U)
        else:
            constants.append([K.cast_to_floatx(1.) for _ in range(4)])

        # States[5]
        if 0 < self.dropout_W < 1:
            input_shape = self.input_spec[1].shape
            input_dim = input_shape[-1]
            ones = K.ones_like(K.reshape(x[:, 0, 0], (-1, 1)))
            ones = K.concatenate([ones] * input_dim, 1)
            B_W = [K.in_train_phase(K.dropout(ones, self.dropout_W), ones) for _ in range(4)]
            constants.append(B_W)
        else:
            constants.append([K.cast_to_floatx(1.) for _ in range(4)])

        if 0 < self.dropout_V < 1:
            input_dim = self.input_dim
            ones = K.ones_like(K.reshape(x[:, :, 0], (-1, x.shape[1], 1))) # (bs, timesteps, 1)
            ones = K.concatenate([ones] * input_dim, axis=2)
            B_V = [K.in_train_phase(K.dropout(ones, self.dropout_V), ones) for _ in range(4)]
        else:
            B_V = [K.cast_to_floatx(1.) for _ in range(4)]

        # AttModel
        # States[6]
        if 0 < self.dropout_wa < 1:
            ones = K.ones_like(K.reshape(self.context[:, :, 0], (-1, self.context.shape[1], 1)))
            #ones = K.concatenate([ones], 1)
            B_wa = [K.in_train_phase(K.dropout(ones, self.dropout_wa), ones)]
            constants.append(B_wa)
        else:
            constants.append([K.cast_to_floatx(1.)])

        # States[7]
        if 0 < self.dropout_Wa < 1:
            input_dim = self.output_dim
            ones = K.ones_like(K.reshape(x[:, 0, 0], (-1, 1)))
            ones = K.concatenate([ones] * input_dim, 1)
            B_Wa = [K.in_train_phase(K.dropout(ones, self.dropout_Wa), ones)]
            constants.append(B_Wa)
        else:
            constants.append([K.cast_to_floatx(1.)])

        if 0 < self.dropout_Ua < 1:
            input_dim = self.context_dim
            ones = K.ones_like(K.reshape(self.context[:, :, 0], (-1, self.context.shape[1], 1)))
            ones = K.concatenate([ones] * input_dim, axis=2)
            B_Ua = [K.in_train_phase(K.dropout(ones, self.dropout_Ua), ones)]
            pctx = K.dot(self.context * B_Ua[0], self.Ua) + self.ba
        else:
            pctx = K.dot(self.context, self.Ua) + self.ba

        # States[8]
        constants.append(pctx)

        # States[9]
        constants.append(self.context)

        # States[10]
        if mask_context is None:
            mask_context = K.not_equal(K.sum(self.context, axis=2), self.mask_value)
        constants.append(mask_context)

        return constants, B_V

    def get_initial_states(self, x):
        # build an all-zero tensor of shape (samples, output_dim)
        if self.init_state is None:
            initial_state = K.zeros_like(x)  # (samples, timesteps, input_dim)
            initial_state = K.sum(initial_state, axis=(1, 2))  # (samples,)
            initial_state = K.expand_dims(initial_state)  # (samples, 1)
            initial_state = K.tile(initial_state, [1, self.output_dim])  # (samples, output_dim)
            if self.init_memory is None:
                initial_states = [initial_state for _ in range(2)]
            else:
                initial_memory = self.init_memory
                initial_states = [initial_state, initial_memory]
        else:
            initial_state = self.init_state
            if self.init_memory is not None: # We have state and memory
                initial_memory = self.init_memory
                initial_states = [initial_state, initial_memory]
            else:
                initial_states = [initial_state for _ in range(2)]

        initial_state = K.zeros_like(self.context)            # (samples, intput_timesteps, ctx_dim)
        initial_state_alphas = K.sum(initial_state, axis=2)   # (samples, input_timesteps)
        initial_state = K.sum(initial_state, axis=1)          # (samples, ctx_dim)
        extra_states = [initial_state, initial_state_alphas]  # (samples, ctx_dim)

        return initial_states + extra_states

    def get_config(self):
        config = {'output_dim': self.output_dim,
                  'return_extra_variables': self.return_extra_variables,
                  'return_states': self.return_states,
                  'init': self.init.__name__,
                  'inner_init': self.inner_init.__name__,
                  'forget_bias_init': self.forget_bias_init.__name__,
                  'activation': self.activation.__name__,
                  'inner_activation': self.inner_activation.__name__,
                  'mask_value': self.mask_value,
                  'W_regularizer': self.W_regularizer.get_config() if self.W_regularizer else None,
                  'U_regularizer': self.U_regularizer.get_config() if self.U_regularizer else None,
                  'V_regularizer': self.V_regularizer.get_config() if self.V_regularizer else None,
                  'b_regularizer': self.b_regularizer.get_config() if self.b_regularizer else None,
                  'wa_regularizer': self.wa_regularizer.get_config() if self.wa_regularizer else None,
                  'Wa_regularizer': self.Wa_regularizer.get_config() if self.Wa_regularizer else None,
                  'Ua_regularizer': self.Ua_regularizer.get_config() if self.Ua_regularizer else None,
                  'ba_regularizer': self.ba_regularizer.get_config() if self.ba_regularizer else None,
                  'ca_regularizer': self.ca_regularizer.get_config() if self.ca_regularizer else None,
                  'dropout_W': self.dropout_W,
                  'dropout_U': self.dropout_U,
                  'dropout_V': self.dropout_V,
                  'dropout_wa': self.dropout_wa,
                  'dropout_Wa': self.dropout_Wa,
                  'dropout_Ua': self.dropout_Ua}
        base_config = super(AttLSTMCond, self).get_config()
        return dict(list(base_config.items()) + list(config.items()))

class AttLSTMCond2Inputs(Recurrent):
    '''Conditional LSTM: The previously generated word is fed to the current timestep

    # Arguments
        output_dim: dimension of the internal projections and the final output.
        init: weight initialization function.
            Can be the name of an existing function (str),
            or a Theano function (see: [initializations](../initializations.md)).
        inner_init: initialization function of the inner cells.
        return_states: boolean indicating if we want the intermediate states (hidden_state and memory) as additional outputs
        forget_bias_init: initialization function for the bias of the forget gate.
            [Jozefowicz et al.](http://www.jmlr.org/proceedings/papers/v37/jozefowicz15.pdf)
            recommend initializing with ones.
        activation: activation function.
            Can be the name of an existing function (str),
            or a Theano function (see: [activations](../activations.md)).
        inner_activation: activation function for the inner cells.
        W_regularizer: instance of [WeightRegularizer](../regularizers.md)
            (eg. L1 or L2 regularization), applied to the input weights matrices.
        U_regularizer: instance of [WeightRegularizer](../regularizers.md)
            (eg. L1 or L2 regularization), applied to the recurrent weights matrices.
        b_regularizer: instance of [WeightRegularizer](../regularizers.md),
            applied to the bias.
        dropout_W: float between 0 and 1. Fraction of the input units to drop for input gates.
        dropout_U: float between 0 and 1. Fraction of the input units to drop for recurrent connections.

    # References
        - [Long short-term memory](http://deeplearning.cs.cmu.edu/pdfs/Hochreiter97_lstm.pdf) (original 1997 paper)
        - [Learning to forget: Continual prediction with LSTM](http://www.mitpressjournals.org/doi/pdf/10.1162/089976600300015015)
        - [Supervised sequence labelling with recurrent neural networks](http://www.cs.toronto.edu/~graves/preprint.pdf)
        - [A Theoretically Grounded Application of Dropout in Recurrent Neural Networks](http://arxiv.org/abs/1512.05287)
    '''
    def __init__(self, output_dim,
                 init='glorot_uniform', inner_init='orthogonal', init_att='glorot_uniform',
                 return_states=False, return_extra_variables=False, attend_on_both=False,
                 forget_bias_init='one', activation='tanh',
                 inner_activation='hard_sigmoid', consume_less='gpu', mask_value=0.,
                 T_regularizer=None, W_regularizer=None, V_regularizer=None, U_regularizer=None, b_regularizer=None,
                 wa_regularizer=None, Wa_regularizer=None, Ua_regularizer=None, ba_regularizer=None, ca_regularizer=None,
                 wa2_regularizer=None, Wa2_regularizer=None, Ua2_regularizer=None, ba2_regularizer=None, ca2_regularizer=None,
                 dropout_T=0., dropout_W=0., dropout_U=0., dropout_V=0.,
                 dropout_wa=0., dropout_Wa=0., dropout_Ua=0.,
                 dropout_wa2=0., dropout_Wa2=0., dropout_Ua2=0.,**kwargs):
        self.output_dim = output_dim
        self.return_extra_variables = return_extra_variables
        self.return_states = return_states
        self.init = initializations.get(init)
        self.inner_init = initializations.get(inner_init)
        self.init_att = initializations.get(init_att)
        self.forget_bias_init = initializations.get(forget_bias_init)
        self.activation = activations.get(activation)
        self.inner_activation = activations.get(inner_activation)
        self.consume_less = consume_less
        self.mask_value = mask_value
        self.attend_on_both = attend_on_both
        # Dropouts
        self.dropout_T, self.dropout_W, self.dropout_U, self.dropout_V = dropout_T, dropout_W, dropout_U, dropout_V
        self.dropout_wa, self.dropout_Wa, self.dropout_Ua = dropout_wa, dropout_Wa, dropout_Ua
        if self.attend_on_both:
            self.dropout_wa2, self.dropout_Wa2, self.dropout_Ua2 = dropout_wa2, dropout_Wa2, dropout_Ua2

        if self.dropout_T or self.dropout_W or self.dropout_U or self.dropout_V or self.dropout_wa or \
                self.dropout_Wa or self.dropout_Ua:
            self.uses_learning_phase = True
        if self.attend_on_both and (self.dropout_wa2 or self.dropout_Wa2 or self.dropout_Ua2):
            self.uses_learning_phase = True

        # Regularizers
        self.T_regularizer = regularizers.get(T_regularizer)
        self.W_regularizer = regularizers.get(W_regularizer)
        self.V_regularizer = regularizers.get(V_regularizer)
        self.U_regularizer = regularizers.get(U_regularizer)
        self.b_regularizer = regularizers.get(b_regularizer)
        # attention model learnable params
        self.wa_regularizer = regularizers.get(wa_regularizer)
        self.Wa_regularizer = regularizers.get(Wa_regularizer)
        self.Ua_regularizer = regularizers.get(Ua_regularizer)
        self.ba_regularizer = regularizers.get(ba_regularizer)
        self.ca_regularizer = regularizers.get(ca_regularizer)
        if self.attend_on_both:
            # attention model 2 learnable params
            self.wa2_regularizer = regularizers.get(wa2_regularizer)
            self.Wa2_regularizer = regularizers.get(Wa2_regularizer)
            self.Ua2_regularizer = regularizers.get(Ua2_regularizer)
            self.ba2_regularizer = regularizers.get(ba2_regularizer)
            self.ca2_regularizer = regularizers.get(ca2_regularizer)
        super(AttLSTMCond2Inputs, self).__init__(**kwargs)

    def build(self, input_shape):
        assert len(input_shape) >= 3 or'You should pass two inputs to AttLSTMCond2Inputs ' \
                                       '(previous_embedded_words, context1 and context2) and ' \
                                       'two optional inputs (init_state and init_memory)'

        if len(input_shape) == 3:
            self.input_spec = [InputSpec(shape=input_shape[0]),
                               InputSpec(shape=input_shape[1]),
                               InputSpec(shape=input_shape[2])]
            self.num_inputs = 3
        elif len(input_shape) == 5:
            self.input_spec = [InputSpec(shape=input_shape[0]),
                               InputSpec(shape=input_shape[1]),
                               InputSpec(shape=input_shape[2]),
                               InputSpec(shape=input_shape[3]),
                               InputSpec(shape=input_shape[4])]
            self.num_inputs = 5
        self.input_dim = input_shape[0][2]

        if self.attend_on_both:
            assert self.input_spec[1].ndim == 3 and self.input_spec[2].ndim, 'When using two attention models,' \
                                                                             'you should pass two 3D tensors' \
                                                                             'to AttLSTMCond2Inputs'
        else:
            assert self.input_spec[1].ndim == 3, 'When using an attention model, you should pass one 3D tensors' \
                                                                             'to AttLSTMCond2Inputs'

        if self.input_spec[1].ndim == 3:
            self.context1_steps = input_shape[1][1]
            self.context1_dim = input_shape[1][2]

        if self.input_spec[2].ndim == 3:
            self.context2_steps = input_shape[2][1]
            self.context2_dim = input_shape[2][2]

        else:
            self.context2_dim = input_shape[2][1]

        if self.stateful:
            self.reset_states()
        else:
            # initial states: 2 all-zero tensors of shape (output_dim)
            self.states = [None, None, None, None] # if self.attend_on_both else [None, None, None]# [h, c, x_att, x_att2]

        # Initialize Att model params (following the same format for any option of self.consume_less)
        self.wa = self.add_weight((self.context1_dim,),
                                   initializer=self.init_att,
                                   name='{}_wa'.format(self.name),
                                   regularizer=self.wa_regularizer)

        self.Wa = self.add_weight((self.output_dim, self.context1_dim),
                                   initializer=self.init_att,
                                   name='{}_Wa'.format(self.name),
                                   regularizer=self.Wa_regularizer)
        self.Ua = self.add_weight((self.context1_dim, self.context1_dim),
                                   initializer=self.inner_init,
                                   name='{}_Ua'.format(self.name),
                                   regularizer=self.Ua_regularizer)

        self.ba = self.add_weight(self.context1_dim,
                                   initializer='zero',
                                   name='{}_ca'.format(self.name),
                                   regularizer=self.ba_regularizer)

        self.ca = self.add_weight(self.context1_steps,
                                  initializer='zero',
                                   name='{}_ca'.format(self.name),
                                  regularizer=self.ca_regularizer)

        if self.attend_on_both:
                    # Initialize Att model params (following the same format for any option of self.consume_less)
            self.wa2 = self.add_weight((self.context2_dim,),
                                       initializer=self.init,
                                       name='{}_wa2'.format(self.name),
                                       regularizer=self.wa2_regularizer)

            self.Wa2 = self.add_weight((self.output_dim, self.context2_dim),
                                       initializer=self.init,
                                       name='{}_Wa2'.format(self.name),
                                       regularizer=self.Wa2_regularizer)
            self.Ua2 = self.add_weight((self.context2_dim, self.context2_dim),
                                       initializer=self.inner_init,
                                       name='{}_Ua2'.format(self.name),
                                       regularizer=self.Ua2_regularizer)

            self.ba2 = self.add_weight(self.context2_dim,
                                       initializer='zero',
                                       regularizer=self.ba2_regularizer)

            self.ca2 = self.add_weight(self.context2_steps,
                                      initializer='zero',
                                      regularizer=self.ca2_regularizer)

        if self.consume_less == 'gpu':

            self.T = self.add_weight((self.context1_dim, 4 * self.output_dim),
                                     initializer=self.init,
                                     name='{}_T'.format(self.name),
                                     regularizer=self.W_regularizer)
            self.W = self.add_weight((self.context2_dim, 4 * self.output_dim),
                                     initializer=self.init,
                                     name='{}_W'.format(self.name),
                                     regularizer=self.W_regularizer)
            self.U = self.add_weight((self.output_dim, 4 * self.output_dim),
                                     initializer=self.inner_init,
                                     name='{}_U'.format(self.name),
                                     regularizer=self.U_regularizer)
            self.V = self.add_weight((self.input_dim, 4 * self.output_dim),
                                     initializer=self.init,
                                     name='{}_V'.format(self.name),
                                     regularizer=self.V_regularizer)

            def b_reg(shape, name=None):
                return K.variable(np.hstack((np.zeros(self.output_dim),
                                             K.get_value(self.forget_bias_init((self.output_dim,))),
                                             np.zeros(self.output_dim),
                                             np.zeros(self.output_dim))),
                                  name='{}_b'.format(self.name))
            self.b = self.add_weight((self.output_dim * 4,),
                                     initializer=b_reg,
                                     name='{}_b'.format(self.name),
                                     regularizer=self.b_regularizer)
            self.trainable_weights = [self.wa, self.Wa, self.Ua, self.ba, self.ca,  # AttModel parameters
                                      self.T,
                                      self.W,
                                      self.U,
                                      self.V,
                                      self.b]
            if self.attend_on_both:
                self.trainable_weights += [self.wa2, self.Wa2, self.Ua2, self.ba2, self.ca2]  # AttModel2 parameters)

        else:
            raise NotImplementedError

        if self.initial_weights is not None:
            self.set_weights(self.initial_weights)
            del self.initial_weights
        self.built = True

    def reset_states(self):
        assert self.stateful, 'Layer must be stateful.'
        input_shape = self.input_spec[0][0].shape
        if not input_shape[0]:
            raise Exception('If a RNN is stateful, a complete ' +
                            'input_shape must be provided (including batch size).')
        if hasattr(self, 'states'):
            K.set_value(self.states[0],
                        np.zeros((input_shape[0], self.output_dim)))
            K.set_value(self.states[1],
                        np.zeros((input_shape[0], self.output_dim)))
            K.set_value(self.states[2],
                        np.zeros((input_shape[0], input_shape[3])))
        else:
            self.states = [K.zeros((input_shape[0], self.output_dim)),
                           K.zeros((input_shape[0], self.output_dim)),
                           K.zeros((input_shape[0], input_shape[3]))]

    def preprocess_input(self, x, B_V):
        return K.dot(x * B_V[0], self.V)

    def get_output_shape_for(self, input_shape):
        if self.return_sequences:
            main_out = (input_shape[0][0], input_shape[0][1], self.output_dim)
        else:
            main_out = (input_shape[0][0], self.output_dim)

        if self.return_extra_variables:
            dim_x_att = (input_shape[0][0], input_shape[0][1], self.context1_dim)
            dim_alpha_att = (input_shape[0][0], input_shape[0][1], input_shape[1][1])
            dim_x_att2 = (input_shape[0][0], input_shape[0][1], self.context2_dim)
            dim_alpha_att2 = (input_shape[0][0], input_shape[0][1], input_shape[2][1])
            main_out = [main_out, dim_x_att, dim_alpha_att, dim_x_att2, dim_alpha_att2]

        if self.return_states:
            if not isinstance(main_out, list):
                main_out = [main_out]
            states_dim = (input_shape[0][0], input_shape[0][1], self.output_dim)
            main_out += [states_dim, states_dim]

        return main_out

    def call(self, x, mask=None):
        # input shape: (nb_samples, time (padded with zeros), input_dim)
        # note that the .build() method of subclasses MUST define
        # self.input_spec with a complete input shape.

        input_shape = self.input_spec[0].shape
        state_below = x[0]
        self.context1 = x[1]
        self.context2 = x[2]
        if self.num_inputs == 3: # input: [state_below, context]
            self.init_state = None
            self.init_memory = None
        elif self.num_inputs == 4: # input: [state_below, context, init_generic]
            self.init_state = x[3]
            self.init_memory = x[3]
        elif self.num_inputs == 5: # input: [state_below, context, init_state, init_memory]
            self.init_state = x[3]
            self.init_memory = x[4]
        if K._BACKEND == 'tensorflow':
            if not input_shape[1]:
                raise Exception('When using TensorFlow, you should define '
                                'explicitly the number of timesteps of '
                                'your sequences.\n'
                                'If your first layer is an Embedding, '
                                'make sure to pass it an "input_length" '
                                'argument. Otherwise, make sure '
                                'the first layer has '
                                'an "input_shape" or "batch_input_shape" '
                                'argument, including the time axis. '
                                'Found input shape at layer ' + self.name +
                                ': ' + str(input_shape))
        if self.stateful:
            initial_states = self.states
        else:
            initial_states = self.get_initial_states(state_below)
        constants, B_V = self.get_constants(state_below, mask[1], mask[2])

        preprocessed_input = self.preprocess_input(state_below, B_V)

        last_output, outputs, states = K.rnn(self.step,
                                             preprocessed_input,
                                             initial_states,
                                             go_backwards=self.go_backwards,
                                             mask=mask[0],
                                             constants=constants,
                                             unroll=self.unroll,
                                             input_length=state_below.shape[1],
                                             pos_extra_outputs_states=[2, 3, 4, 5])
        if self.stateful:
            self.updates = []
            for i in range(len(states)):
                self.updates.append((self.states[i], states[i]))
        if self.return_sequences:
            ret = outputs
        else:
            ret = last_output
        if self.return_extra_variables:
            ret = [ret, states[2], states[3], states[4], states[5]]
        # intermediate states as additional outputs
        if self.return_states:
            if not isinstance(ret, list):
                ret = [ret]
            ret += [states[0], states[1]]

        return ret

    def compute_mask(self, input, mask):

        if self.return_extra_variables:
            ret = [mask[0], mask[0], mask[0], mask[0], mask[0]]
        else:
            ret = mask[0]

        #if self.return_sequences:
        #    ret = mask[0]
        #else:
        #    ret = None
        if self.return_states:
            if not isinstance(ret, list):
                ret = [ret]
            ret += [mask[0], mask[0]]
        return ret

    def step(self, x, states):
        h_tm1 = states[0]                               # State
        c_tm1 = states[1]                               # Memory
        pos_states = 11

        non_used_x_att = states[2]                      # Placeholder for returning extra variables
        non_used_alphas_att = states[3]                 # Placeholder for returning extra variables
        non_used_x_att2 = states[4]                     # Placeholder for returning extra variables
        non_used_alphas_att2 = states[5]                # Placeholder for returning extra variables

        B_U = states[6]                                 # Dropout U
        B_T = states[7]                                 # Dropout T
        B_W = states[8]                                 # Dropout W

        # Att model dropouts
        B_wa = states[9]                                # Dropout wa
        B_Wa = states[10]                               # Dropout Wa
        # Att model 2 dropouts
        if self.attend_on_both:
            B_wa2 = states[pos_states]                  # Dropout wa
            B_Wa2 = states[pos_states+1]                # Dropout Wa

            context1 = states[pos_states+2]             # Context
            mask_context1 = states[pos_states+3]        # Context mask
            pctx_1 = states[pos_states+4]               # Projected context (i.e. context * Ua + ba)

            context2 = states[pos_states+5]             # Context 2
            mask_context2 = states[pos_states+6]        # Context 2 mask
            pctx_2 = states[pos_states+7]               # Projected context 2 (i.e. context * Ua2 + ba2)
        else:
            context1 = states[pos_states]               # Context
            mask_context1 = states[pos_states+1]        # Context mask
            pctx_1 = states[pos_states+2]               # Projected context (i.e. context * Ua + ba)

            context2 = states[pos_states+3]             # Context 2
            mask_context2 = states[pos_states+4]        # Context 2 mask

        if mask_context1.ndim > 1:                      # Mask the context (only if necessary)
            pctx_1 = mask_context1[:, :, None] * pctx_1
            context1 = mask_context1[:, :, None] * context1

        # Attention model 1 (see Formulation in class header)
        p_state_1 = K.dot(h_tm1 * B_Wa[0], self.Wa)
        pctx_1 = K.tanh(pctx_1 + p_state_1[:, None, :])
        e1 = K.dot(pctx_1 * B_wa[0], self.wa) + self.ca
        if mask_context1.ndim > 1: # Mask the context (only if necessary)
            e1 = mask_context1 * e1
        alphas_shape1 = e1.shape
        alphas1 = K.softmax(e1.reshape([alphas_shape1[0], alphas_shape1[1]]))
        # sum over the in_timesteps dimension resulting in [batch_size, input_dim]
        ctx_1 = (context1 * alphas1[:, :, None]).sum(axis=1)

        if self.attend_on_both and mask_context2.ndim > 1:  # Mask the context2 (only if necessary)
            pctx_2 = mask_context2[:, :, None] * pctx_2
            context2 = mask_context2[:, :, None] * context2

        if self.attend_on_both:
            # Attention model 2 (see Formulation in class header)
            p_state_2 = K.dot(h_tm1 * B_Wa2[0], self.Wa2)
            pctx_2 = K.tanh(pctx_2 + p_state_2[:, None, :])
            e2 = K.dot(pctx_2 * B_wa2[0], self.wa2) + self.ca2
            if mask_context2.ndim > 1: # Mask the context (only if necessary)
                e2 = mask_context2 * e2
            alphas_shape2 = e2.shape
            alphas2 = K.softmax(e2.reshape([alphas_shape2[0], alphas_shape2[1]]))
            # sum over the in_timesteps dimension resulting in [batch_size, input_dim]
            ctx_2 = (context2 * alphas2[:, :, None]).sum(axis=1)
        else:
            ctx_2 = context2
            alphas2 = mask_context2

        z = x + \
            K.dot(h_tm1 * B_U[0], self.U)  + \
            K.dot(ctx_1 * B_T[0], self.T) + \
            K.dot(ctx_2 * B_W[0], self.W) + \
            self.b
        z0 = z[:, :self.output_dim]
        z1 = z[:, self.output_dim: 2 * self.output_dim]
        z2 = z[:, 2 * self.output_dim: 3 * self.output_dim]
        z3 = z[:, 3 * self.output_dim:]

        i = self.inner_activation(z0)
        f = self.inner_activation(z1)
        c = f * c_tm1 + i * self.activation(z2)
        o = self.inner_activation(z3)
        h = o * self.activation(c)
        return h, [h, c, ctx_1, alphas1, ctx_2, alphas2]

    def get_constants(self, x, mask_context1, mask_context2):
        constants = []
        # States[6]
        if 0 < self.dropout_U < 1:
            ones = K.ones_like(K.reshape(x[:, 0, 0], (-1, 1)))
            ones = K.concatenate([ones] * self.output_dim, 1)
            B_U = [K.in_train_phase(K.dropout(ones, self.dropout_U), ones) for _ in range(4)]
            constants.append(B_U)
        else:
            constants.append([K.cast_to_floatx(1.) for _ in range(4)])

        # States[7]
        if 0 < self.dropout_T < 1:
            input_shape = self.input_spec[1][0].shape
            input_dim = input_shape[-1]
            ones = K.ones_like(K.reshape(x[:, 0, 0], (-1, 1)))
            ones = K.concatenate([ones] * input_dim, 1)
            B_T = [K.in_train_phase(K.dropout(ones, self.dropout_T), ones) for _ in range(4)]
            constants.append(B_T)
        else:
            B_T = [K.cast_to_floatx(1.) for _ in range(4)]
        constants.append(B_T)

        # States[8]
        if 0 < self.dropout_W < 1:
            input_shape = self.input_spec[2][0].shape
            input_dim = input_shape[-1]
            ones = K.ones_like(K.reshape(x[:, 0, 0], (-1, 1)))
            ones = K.concatenate([ones] * input_dim, 1)
            B_W = [K.in_train_phase(K.dropout(ones, self.dropout_W), ones) for _ in range(4)]
            constants.append(B_W)
        else:
            B_W = [K.cast_to_floatx(1.) for _ in range(4)]
        constants.append(B_W)

        # AttModel
        # States[9]
        if 0 < self.dropout_wa < 1:
            ones = K.ones_like(K.reshape(self.context1[:, :, 0], (-1, self.context1.shape[1], 1)))
            #ones = K.concatenate([ones], 1)
            B_wa = [K.in_train_phase(K.dropout(ones, self.dropout_wa), ones)]
            constants.append(B_wa)
        else:
            constants.append([K.cast_to_floatx(1.)])

        # States[10]
        if 0 < self.dropout_Wa < 1:
            input_dim = self.output_dim
            ones = K.ones_like(K.reshape(x[:, 0, 0], (-1, 1)))
            ones = K.concatenate([ones] * input_dim, 1)
            B_Wa = [K.in_train_phase(K.dropout(ones, self.dropout_Wa), ones)]
            constants.append(B_Wa)
        else:
            constants.append([K.cast_to_floatx(1.)])

        if self.attend_on_both:
            # AttModel2
            # States[11]
            if 0 < self.dropout_wa2 < 1:
                ones = K.ones_like(K.reshape(self.context2[:, :, 0], (-1, self.context2.shape[1], 1)))
                #ones = K.concatenate([ones], 1)
                B_wa2 = [K.in_train_phase(K.dropout(ones, self.dropout_wa2), ones)]
                constants.append(B_wa2)
            else:
                constants.append([K.cast_to_floatx(1.)])

            # States[12]
            if 0 < self.dropout_Wa2 < 1:
                input_dim = self.output_dim
                ones = K.ones_like(K.reshape(x[:, 0, 0], (-1, 1)))
                ones = K.concatenate([ones] * input_dim, 1)
                B_Wa2 = [K.in_train_phase(K.dropout(ones, self.dropout_Wa2), ones)]
                constants.append(B_Wa2)
            else:
                constants.append([K.cast_to_floatx(1.)])

        # States[13] - [11]
        constants.append(self.context1)
        # States [14] - [12]
        if mask_context1 is None:
            mask_context1 = K.not_equal(K.sum(self.context1, axis=2), self.mask_value)
        constants.append(mask_context1)

        # States [15] - [13]
        if 0 < self.dropout_Ua < 1:
            input_dim = self.context1_dim
            ones = K.ones_like(K.reshape(self.context1[:, :, 0], (-1, self.context1.shape[1], 1)))
            ones = K.concatenate([ones] * input_dim, axis=2)
            B_Ua = [K.in_train_phase(K.dropout(ones, self.dropout_Ua), ones)]
            pctx1 = K.dot(self.context1 * B_Ua[0], self.Ua) + self.ba
        else:
            pctx1 = K.dot(self.context1, self.Ua) + self.ba
        constants.append(pctx1)

        # States[16] - [14]
        constants.append(self.context2)
        # States [17] - [15]
        if self.attend_on_both:
            if mask_context2 is None:
                mask_context2 = K.not_equal(K.sum(self.context2, axis=2), self.mask_value)
        else:
            mask_context2 = K.ones_like(self.context2[:, 0])
        constants.append(mask_context2)

        # States [18] - [16]
        if self.attend_on_both:
            if 0 < self.dropout_Ua2 < 1:
                input_dim = self.context2_dim
                ones = K.ones_like(K.reshape(self.context2[:, :, 0], (-1, self.context2.shape[1], 1)))
                ones = K.concatenate([ones] * input_dim, axis=2)
                B_Ua2 = [K.in_train_phase(K.dropout(ones, self.dropout_Ua2), ones)]
                pctx2 = K.dot(self.context2 * B_Ua2[0], self.Ua2) + self.ba2
            else:
                pctx2 = K.dot(self.context2, self.Ua2) + self.ba2
            constants.append(pctx2)

        if 0 < self.dropout_V < 1:
            input_dim = self.input_dim
            ones = K.ones_like(K.reshape(x[:, :, 0], (-1, x.shape[1], 1)))
            ones = K.concatenate([ones] * input_dim, axis=2)
            B_V = [K.in_train_phase(K.dropout(ones, self.dropout_V), ones) for _ in range(4)]
        else:
            B_V = [K.cast_to_floatx(1.) for _ in range(4)]
        return constants, B_V

    def get_initial_states(self, x):
        # build an all-zero tensor of shape (samples, output_dim)
        if self.init_state is None:
            # build an all-zero tensor of shape (samples, output_dim)
            initial_state = K.zeros_like(x)  # (samples, timesteps, input_dim)
            initial_state = K.sum(initial_state, axis=(1, 2))  # (samples,)
            initial_state = K.expand_dims(initial_state)  # (samples, 1)
            initial_state = K.tile(initial_state, [1, self.output_dim])  # (samples, output_dim)
            if self.init_memory is None:
                initial_states = [initial_state for _ in range(2)]
            else:
                initial_memory = self.init_memory
                initial_states = [initial_state, initial_memory]
        else:
            initial_state = self.init_state
            if self.init_memory is not None: # We have state and memory
                initial_memory = self.init_memory
                initial_states = [initial_state, initial_memory]
            else:
                initial_states = [initial_state for _ in range(2)]

        # extra states for context1 and context2
        initial_state1 = K.zeros_like(self.context1)  # (samples, input_timesteps, ctx1_dim)
        initial_state_alphas1 = K.sum(initial_state1, axis=2)  # (samples, input_timesteps)
        initial_state1 = K.sum(initial_state1, axis=1)  # (samples, ctx1_dim)
        extra_states = [initial_state1, initial_state_alphas1]
        initial_state2 = K.zeros_like(self.context2)  # (samples, input_timesteps, ctx2_dim)
        if self.attend_on_both:  # Reduce on temporal dimension
            initial_state_alphas2 = K.sum(initial_state2, axis=2)  # (samples, input_timesteps)
            initial_state2 = K.sum(initial_state2, axis=1)  # (samples, ctx2_dim)
        else:  # Already reduced
            initial_state_alphas2 = initial_state2 # (samples, ctx2_dim)

        extra_states.append(initial_state2)
        extra_states.append(initial_state_alphas2)

        return initial_states + extra_states


    def get_config(self):
        config = {"output_dim": self.output_dim,
                  "return_extra_variables": self.return_extra_variables,
                  "return_states": self.return_states,
                  "mask_value": self.mask_value,
                  "attend_on_both": self.attend_on_both,
                  "init": self.init.__name__,
                  "inner_init": self.inner_init.__name__,
                  "forget_bias_init": self.forget_bias_init.__name__,
                  "activation": self.activation.__name__,
                  "inner_activation": self.inner_activation.__name__,
                  "T_regularizer": self.T_regularizer.get_config() if self.T_regularizer else None,
                  "W_regularizer": self.W_regularizer.get_config() if self.W_regularizer else None,
                  "V_regularizer": self.V_regularizer.get_config() if self.V_regularizer else None,
                  "U_regularizer": self.U_regularizer.get_config() if self.U_regularizer else None,
                  "b_regularizer": self.b_regularizer.get_config() if self.b_regularizer else None,
                  'wa_regularizer': self.wa_regularizer.get_config() if self.wa_regularizer else None,
                  'Wa_regularizer': self.Wa_regularizer.get_config() if self.Wa_regularizer else None,
                  'Ua_regularizer': self.Ua_regularizer.get_config() if self.Ua_regularizer else None,
                  'ba_regularizer': self.ba_regularizer.get_config() if self.ba_regularizer else None,
                  'ca_regularizer': self.ca_regularizer.get_config() if self.ca_regularizer else None,
                  'wa2_regularizer': self.wa2_regularizer.get_config() if self.attend_on_both and self.wa2_regularizer else None,
                  'Wa2_regularizer': self.Wa2_regularizer.get_config() if self.attend_on_both and self.Wa2_regularizer else None,
                  'Ua2_regularizer': self.Ua2_regularizer.get_config() if self.attend_on_both and self.Ua2_regularizer else None,
                  'ba2_regularizer': self.ba2_regularizer.get_config() if self.attend_on_both and self.ba2_regularizer else None,
                  'ca2_regularizer': self.ca2_regularizer.get_config() if self.attend_on_both and self.ca2_regularizer else None,
                  "dropout_T": self.dropout_T,
                  "dropout_W": self.dropout_W,
                  "dropout_U": self.dropout_U,
                  "dropout_V": self.dropout_V,
                  'dropout_wa': self.dropout_wa,
                  'dropout_Wa': self.dropout_Wa,
                  'dropout_Ua': self.dropout_Ua,
                  'dropout_wa2': self.dropout_wa2 if self.attend_on_both else None,
                  'dropout_Wa2': self.dropout_Wa2 if self.attend_on_both else None,
                  'dropout_Ua2': self.dropout_Ua2 if self.attend_on_both else None}
        base_config = super(AttLSTMCond2Inputs, self).get_config()
        return dict(list(base_config.items()) + list(config.items()))

class AttLSTMCond3Inputs(Recurrent):
    '''Conditional LSTM: The previously generated word is fed to the current timestep

    # Arguments
        output_dim: dimension of the internal projections and the final output.
        init: weight initialization function.
            Can be the name of an existing function (str),
            or a Theano function (see: [initializations](../initializations.md)).
        inner_init: initialization function of the inner cells.
        return_states: boolean indicating if we want the intermediate states (hidden_state and memory) as additional outputs
        forget_bias_init: initialization function for the bias of the forget gate.
            [Jozefowicz et al.](http://www.jmlr.org/proceedings/papers/v37/jozefowicz15.pdf)
            recommend initializing with ones.
        activation: activation function.
            Can be the name of an existing function (str),
            or a Theano function (see: [activations](../activations.md)).
        inner_activation: activation function for the inner cells.
        W_regularizer: instance of [WeightRegularizer](../regularizers.md)
            (eg. L1 or L2 regularization), applied to the input weights matrices.
        U_regularizer: instance of [WeightRegularizer](../regularizers.md)
            (eg. L1 or L2 regularization), applied to the recurrent weights matrices.
        b_regularizer: instance of [WeightRegularizer](../regularizers.md),
            applied to the bias.
        dropout_W: float between 0 and 1. Fraction of the input units to drop for input gates.
        dropout_U: float between 0 and 1. Fraction of the input units to drop for recurrent connections.

    # References
        - [Long short-term memory](http://deeplearning.cs.cmu.edu/pdfs/Hochreiter97_lstm.pdf) (original 1997 paper)
        - [Learning to forget: Continual prediction with LSTM](http://www.mitpressjournals.org/doi/pdf/10.1162/089976600300015015)
        - [Supervised sequence labelling with recurrent neural networks](http://www.cs.toronto.edu/~graves/preprint.pdf)
        - [A Theoretically Grounded Application of Dropout in Recurrent Neural Networks](http://arxiv.org/abs/1512.05287)
    '''
    def __init__(self, output_dim,
                 init='glorot_uniform', inner_init='orthogonal', init_att='glorot_uniform',
                 return_states=False, return_extra_variables=False, attend_on_both=False,
                 forget_bias_init='one', activation='tanh',
                 inner_activation='hard_sigmoid', consume_less='gpu', mask_value=0.,
                 S_regularizer=None, T_regularizer=None, W_regularizer=None, V_regularizer=None, U_regularizer=None, b_regularizer=None,
                 wa_regularizer=None, Wa_regularizer=None, Ua_regularizer=None, ba_regularizer=None, ca_regularizer=None,
                 wa2_regularizer=None, Wa2_regularizer=None, Ua2_regularizer=None, ba2_regularizer=None, ca2_regularizer=None,
                 wa3_regularizer=None, Wa3_regularizer=None, Ua3_regularizer=None, ba3_regularizer=None, ca3_regularizer=None,
                 dropout_S=0., dropout_T=0., dropout_W=0., dropout_U=0., dropout_V=0.,
                 dropout_wa=0., dropout_Wa=0., dropout_Ua=0.,
                 dropout_wa2=0., dropout_Wa2=0., dropout_Ua2=0.,
                 dropout_wa3=0., dropout_Wa3=0., dropout_Ua3=0.,
                 **kwargs):
        self.output_dim = output_dim
        self.return_extra_variables = return_extra_variables
        self.return_states = return_states
        self.init = initializations.get(init)
        self.inner_init = initializations.get(inner_init)
        self.init_att = initializations.get(init_att)
        self.forget_bias_init = initializations.get(forget_bias_init)
        self.activation = activations.get(activation)
        self.inner_activation = activations.get(inner_activation)
        self.consume_less = consume_less
        self.mask_value = mask_value
        self.attend_on_both = attend_on_both
        # Dropouts
        self.dropout_S, self.dropout_T, self.dropout_W, self.dropout_U, self.dropout_V = \
            dropout_S, dropout_T, dropout_W, dropout_U, dropout_V
        self.dropout_wa, self.dropout_Wa, self.dropout_Ua = dropout_wa, dropout_Wa, dropout_Ua
        if self.attend_on_both:
            self.dropout_wa2, self.dropout_Wa2, self.dropout_Ua2 = dropout_wa2, dropout_Wa2, dropout_Ua2
            self.dropout_wa3, self.dropout_Wa3, self.dropout_Ua3 = dropout_wa3, dropout_Wa3, dropout_Ua3

        if self.dropout_T or self.dropout_W or self.dropout_U or self.dropout_V or self.dropout_wa or \
                self.dropout_Wa or self.dropout_Ua:
            self.uses_learning_phase = True
        if self.attend_on_both and (self.dropout_wa2 or self.dropout_Wa2 or self.dropout_Ua2 or
                                       self.dropout_wa3 or self.dropout_Wa3 or self.dropout_Ua3):
            self.uses_learning_phase = True

        # Regularizers
        self.S_regularizer = regularizers.get(S_regularizer)
        self.T_regularizer = regularizers.get(T_regularizer)
        self.W_regularizer = regularizers.get(W_regularizer)
        self.V_regularizer = regularizers.get(V_regularizer)
        self.U_regularizer = regularizers.get(U_regularizer)
        self.b_regularizer = regularizers.get(b_regularizer)
        # attention model learnable params
        self.wa_regularizer = regularizers.get(wa_regularizer)
        self.Wa_regularizer = regularizers.get(Wa_regularizer)
        self.Ua_regularizer = regularizers.get(Ua_regularizer)
        self.ba_regularizer = regularizers.get(ba_regularizer)
        self.ca_regularizer = regularizers.get(ca_regularizer)
        if self.attend_on_both:
            # attention model learnable params
            self.wa2_regularizer = regularizers.get(wa2_regularizer)
            self.Wa2_regularizer = regularizers.get(Wa2_regularizer)
            self.Ua2_regularizer = regularizers.get(Ua2_regularizer)
            self.ba2_regularizer = regularizers.get(ba2_regularizer)
            self.ca2_regularizer = regularizers.get(ca2_regularizer)

            self.wa3_regularizer = regularizers.get(wa3_regularizer)
            self.Wa3_regularizer = regularizers.get(Wa3_regularizer)
            self.Ua3_regularizer = regularizers.get(Ua3_regularizer)
            self.ba3_regularizer = regularizers.get(ba3_regularizer)
            self.ca3_regularizer = regularizers.get(ca3_regularizer)
        super(AttLSTMCond3Inputs, self).__init__(**kwargs)

    def build(self, input_shape):
        assert len(input_shape) >= 4 or'You should pass three inputs to AttLSTMCond2Inputs ' \
                                       '(previous_embedded_words, context1, context2, context3) and ' \
                                       'two optional inputs (init_state and init_memory)'

        if len(input_shape) == 4:
            self.input_spec = [InputSpec(shape=input_shape[0]),
                               InputSpec(shape=input_shape[1]),
                               InputSpec(shape=input_shape[2]),
                               InputSpec(shape=input_shape[3])]
            self.num_inputs = 4
        elif len(input_shape) == 6:
            self.input_spec = [InputSpec(shape=input_shape[0]),
                               InputSpec(shape=input_shape[1]),
                               InputSpec(shape=input_shape[2]),
                               InputSpec(shape=input_shape[3]),
                               InputSpec(shape=input_shape[4]),
                               InputSpec(shape=input_shape[5])]
            self.num_inputs = 6
        self.input_dim = input_shape[0][2]

        if self.attend_on_both:
            assert self.input_spec[1].ndim == 3 and \
                   self.input_spec[2].ndim == 3 and \
                   self.input_spec[3].ndim == 3, 'When using two attention models,' \
                                                 'you should pass two 3D tensors' \
                                                 'to AttLSTMCond3Inputs'
        else:
            assert self.input_spec[1].ndim == 3, 'When using an attention model, you should pass one 3D tensors' \
                                                                             'to AttLSTMCond3Inputs'

        if self.input_spec[1].ndim == 3:
            self.context1_steps = input_shape[1][1]
            self.context1_dim = input_shape[1][2]

        if self.input_spec[2].ndim == 3:
            self.context2_steps = input_shape[2][1]
            self.context2_dim = input_shape[2][2]

        else:
            self.context2_dim = input_shape[2][1]

        if self.input_spec[3].ndim == 3:
            self.context3_steps = input_shape[3][1]
            self.context3_dim = input_shape[3][2]
        else:
            self.context3_dim = input_shape[3][1]

        if self.stateful:
            self.reset_states()
        else:
            # initial states: 2 all-zero tensors of shape (output_dim)
            self.states = [None, None, None, None, None]

        # Initialize Att model params (following the same format for any option of self.consume_less)
        self.wa = self.add_weight((self.context1_dim,),
                                   initializer=self.init_att,
                                   name='{}_wa'.format(self.name),
                                   regularizer=self.wa_regularizer)

        self.Wa = self.add_weight((self.output_dim, self.context1_dim),
                                   initializer=self.init_att,
                                   name='{}_Wa'.format(self.name),
                                   regularizer=self.Wa_regularizer)
        self.Ua = self.add_weight((self.context1_dim, self.context1_dim),
                                   initializer=self.inner_init,
                                   name='{}_Ua'.format(self.name),
                                   regularizer=self.Ua_regularizer)

        self.ba = self.add_weight(self.context1_dim,
                                   initializer='zero',
                                   name='{}_ba'.format(self.name),
                                   regularizer=self.ba_regularizer)

        self.ca = self.add_weight(self.context1_steps,
                                  initializer='zero',
                                   name='{}_ca'.format(self.name),
                                  regularizer=self.ca_regularizer)

        if self.attend_on_both:
            # Initialize Att model params (following the same format for any option of self.consume_less)
            self.wa2 = self.add_weight((self.context2_dim,),
                                       initializer=self.init,
                                       name='{}_wa2'.format(self.name),
                                       regularizer=self.wa2_regularizer)

            self.Wa2 = self.add_weight((self.output_dim, self.context2_dim),
                                       initializer=self.init,
                                       name='{}_Wa2'.format(self.name),
                                       regularizer=self.Wa2_regularizer)
            self.Ua2 = self.add_weight((self.context2_dim, self.context2_dim),
                                       initializer=self.inner_init,
                                       name='{}_Ua2'.format(self.name),
                                       regularizer=self.Ua2_regularizer)

            self.ba2 = self.add_weight(self.context2_dim,
                                       initializer='zero',
                                       regularizer=self.ba2_regularizer)

            self.ca2 = self.add_weight(self.context2_steps,
                                      initializer='zero',
                                      regularizer=self.ca2_regularizer)

            self.wa3 = self.add_weight((self.context3_dim,),
                                       initializer=self.init,
                                       name='{}_wa3'.format(self.name),
                                       regularizer=self.wa3_regularizer)

            self.Wa3 = self.add_weight((self.output_dim, self.context3_dim),
                                       initializer=self.init,
                                       name='{}_Wa3'.format(self.name),
                                       regularizer=self.Wa3_regularizer)
            self.Ua3 = self.add_weight((self.context3_dim, self.context3_dim),
                                       initializer=self.inner_init,
                                       name='{}_Ua3'.format(self.name),
                                       regularizer=self.Ua3_regularizer)

            self.ba3 = self.add_weight(self.context3_dim,
                                       initializer='zero',
                                       regularizer=self.ba3_regularizer)

            self.ca3 = self.add_weight(self.context3_steps,
                                      initializer='zero',
                                      regularizer=self.ca3_regularizer)

        if self.consume_less == 'gpu':

            self.T = self.add_weight((self.context1_dim, 4 * self.output_dim),
                                     initializer=self.init,
                                     name='{}_T'.format(self.name),
                                     regularizer=self.W_regularizer)
            self.W = self.add_weight((self.context2_dim, 4 * self.output_dim),
                                     initializer=self.init,
                                     name='{}_W'.format(self.name),
                                     regularizer=self.W_regularizer)
            self.S = self.add_weight((self.context3_dim, 4 * self.output_dim),
                                     initializer=self.init,
                                     name='{}_S'.format(self.name),
                                     regularizer=self.S_regularizer)

            self.U = self.add_weight((self.output_dim, 4 * self.output_dim),
                                     initializer=self.inner_init,
                                     name='{}_U'.format(self.name),
                                     regularizer=self.U_regularizer)
            self.V = self.add_weight((self.input_dim, 4 * self.output_dim),
                                     initializer=self.init,
                                     name='{}_V'.format(self.name),
                                     regularizer=self.V_regularizer)

            def b_reg(shape, name=None):
                return K.variable(np.hstack((np.zeros(self.output_dim),
                                             K.get_value(self.forget_bias_init((self.output_dim,))),
                                             np.zeros(self.output_dim),
                                             np.zeros(self.output_dim))),
                                  name='{}_b'.format(self.name))
            self.b = self.add_weight((self.output_dim * 4,),
                                     initializer=b_reg,
                                     name='{}_b'.format(self.name),
                                     regularizer=self.b_regularizer)
            self.trainable_weights = [self.wa, self.Wa, self.Ua, self.ba, self.ca,  # AttModel parameters
                                      self.S,
                                      self.T,
                                      self.W,
                                      self.U,
                                      self.V,
                                      self.b]
            if self.attend_on_both:
                self.trainable_weights += [self.wa2, self.Wa2, self.Ua2, self.ba2, self.ca2, # AttModel2 parameters)
                                           self.wa3, self.Wa3, self.Ua3, self.ba3, self.ca3 # AttModel3 parameters)
                                           ]

        else:
            raise NotImplementedError

        if self.initial_weights is not None:
            self.set_weights(self.initial_weights)
            del self.initial_weights
        self.built = True

    def reset_states(self):
        assert self.stateful, 'Layer must be stateful.'
        input_shape = self.input_spec[0][0].shape
        if not input_shape[0]:
            raise Exception('If a RNN is stateful, a complete ' +
                            'input_shape must be provided (including batch size).')
        if hasattr(self, 'states'):
            K.set_value(self.states[0],
                        np.zeros((input_shape[0], self.output_dim)))
            K.set_value(self.states[1],
                        np.zeros((input_shape[0], self.output_dim)))
            K.set_value(self.states[2],
                        np.zeros((input_shape[0], input_shape[3])))
        else:
            self.states = [K.zeros((input_shape[0], self.output_dim)),
                           K.zeros((input_shape[0], self.output_dim)),
                           K.zeros((input_shape[0], input_shape[3]))]

    def preprocess_input(self, x, B_V):
        return K.dot(x * B_V[0], self.V)

    def get_output_shape_for(self, input_shape):
        if self.return_sequences:
            main_out = (input_shape[0][0], input_shape[0][1], self.output_dim)
        else:
            main_out = (input_shape[0][0], self.output_dim)

        if self.return_extra_variables:
            dim_x_att = (input_shape[0][0], input_shape[0][1], self.context1_dim)
            dim_alpha_att = (input_shape[0][0], input_shape[0][1], input_shape[1][1])
            dim_x_att2 = (input_shape[0][0], input_shape[0][1], self.context2_dim)
            dim_alpha_att2 = (input_shape[0][0], input_shape[0][1], input_shape[2][1])
            dim_x_att3 = (input_shape[0][0], input_shape[0][1], self.context3_dim)
            dim_alpha_att3 = (input_shape[0][0], input_shape[0][1], input_shape[3][1])

            main_out = [main_out, dim_x_att, dim_alpha_att, dim_x_att2, dim_alpha_att2, dim_x_att3, dim_alpha_att3]

        if self.return_states:
            if not isinstance(main_out, list):
                main_out = [main_out]
            states_dim = (input_shape[0][0], input_shape[0][1], self.output_dim)
            main_out += [states_dim, states_dim]

        return main_out

    def call(self, x, mask=None):
        # input shape: (nb_samples, time (padded with zeros), input_dim)
        # note that the .build() method of subclasses MUST define
        # self.input_spec with a complete input shape.

        input_shape = self.input_spec[0].shape
        state_below = x[0]
        self.context1 = x[1]
        self.context2 = x[2]
        self.context3 = x[3]

        if self.num_inputs == 4: # input: [state_below, context, context3]
            self.init_state = None
            self.init_memory = None
        elif self.num_inputs == 5: # input: [state_below, context, context2, init_generic]
            self.init_state = x[4]
            self.init_memory = x[4]
        elif self.num_inputs == 6: # input: [state_below, context, context2,  init_state, init_memory]
            self.init_state = x[4]
            self.init_memory = x[5]
        if K._BACKEND == 'tensorflow':
            if not input_shape[1]:
                raise Exception('When using TensorFlow, you should define '
                                'explicitly the number of timesteps of '
                                'your sequences.\n'
                                'If your first layer is an Embedding, '
                                'make sure to pass it an "input_length" '
                                'argument. Otherwise, make sure '
                                'the first layer has '
                                'an "input_shape" or "batch_input_shape" '
                                'argument, including the time axis. '
                                'Found input shape at layer ' + self.name +
                                ': ' + str(input_shape))
        if self.stateful:
            initial_states = self.states
        else:
            initial_states = self.get_initial_states(state_below)
        constants, B_V = self.get_constants(state_below, mask[1], mask[2], mask[3])

        preprocessed_input = self.preprocess_input(state_below, B_V)

        last_output, outputs, states = K.rnn(self.step,
                                             preprocessed_input,
                                             initial_states,
                                             go_backwards=self.go_backwards,
                                             mask=mask[0],
                                             constants=constants,
                                             unroll=self.unroll,
                                             input_length=state_below.shape[1],
                                             pos_extra_outputs_states=[2, 3, 4, 5, 6, 7])
        if self.stateful:
            self.updates = []
            for i in range(len(states)):
                self.updates.append((self.states[i], states[i]))
        if self.return_sequences:
            ret = outputs
        else:
            ret = last_output
        if self.return_extra_variables:
            ret = [ret, states[2], states[3], states[4], states[5], states[6], states[7]]
        # intermediate states as additional outputs
        if self.return_states:
            if not isinstance(ret, list):
                ret = [ret]
            ret += [states[0], states[1]]

        return ret

    def compute_mask(self, input, mask):

        if self.return_extra_variables:
            ret = [mask[0], mask[0], mask[0], mask[0], mask[0], mask[0], mask[0]]
        else:
            ret = mask[0]

        if self.return_states:
            if not isinstance(ret, list):
                ret = [ret]
            ret += [mask[0], mask[0]]
        return ret

    def step(self, x, states):
        h_tm1 = states[0]                               # State
        c_tm1 = states[1]                               # Memory
        pos_states = 14

        non_used_x_att = states[2]                      # Placeholder for returning extra variables
        non_used_alphas_att = states[3]                 # Placeholder for returning extra variables

        non_used_x_att2 = states[4]                     # Placeholder for returning extra variables
        non_used_alphas_att2 = states[5]                # Placeholder for returning extra variables

        non_used_x_att3 = states[6]                     # Placeholder for returning extra variables
        non_used_alphas_att3 = states[7]                # Placeholder for returning extra variables

        B_U = states[8]                                 # Dropout U
        B_T = states[9]                                 # Dropout T
        B_W = states[10]                                 # Dropout W
        B_S = states[11]                                 # Dropout T

        # Att model dropouts
        B_wa = states[12]                                # Dropout wa
        B_Wa = states[13]                               # Dropout Wa
        # Att model 2 dropouts
        if self.attend_on_both:
            B_wa2 = states[pos_states]                  # Dropout wa
            B_Wa2 = states[pos_states+1]                # Dropout Wa
            B_wa3 = states[pos_states+2]                # Dropout wa3
            B_Wa3 = states[pos_states+3]                # Dropout Wa3

            context1 = states[pos_states+4]             # Context
            mask_context1 = states[pos_states+5]        # Context mask
            pctx_1 = states[pos_states+6]               # Projected context (i.e. context * Ua + ba)

            context2 = states[pos_states+7]             # Context 2
            mask_context2 = states[pos_states+8]        # Context 2 mask
            pctx_2 = states[pos_states+9]               # Projected context 2 (i.e. context * Ua2 + ba2)

            context3 = states[pos_states+10]             # Context 3
            mask_context3 = states[pos_states+11]        # Context 3 mask
            pctx_3 = states[pos_states+12]               # Projected context 3 (i.e. context * Ua3 + ba3)

        else:
            context1 = states[pos_states]               # Context
            mask_context1 = states[pos_states+1]        # Context mask
            pctx_1 = states[pos_states+2]               # Projected context (i.e. context * Ua + ba)

            context2 = states[pos_states+3]             # Context 2
            mask_context2 = states[pos_states+4]        # Context 2 mask

            context3 = states[pos_states+5]             # Context 2
            mask_context3 = states[pos_states+6]        # Context 2 mask

        if mask_context1.ndim > 1:                      # Mask the context (only if necessary)
            pctx_1 = mask_context1[:, :, None] * pctx_1
            context1 = mask_context1[:, :, None] * context1

        # Attention model 1 (see Formulation in class header)
        p_state_1 = K.dot(h_tm1 * B_Wa[0], self.Wa)
        pctx_1 = K.tanh(pctx_1 + p_state_1[:, None, :])
        e1 = K.dot(pctx_1 * B_wa[0], self.wa) + self.ca
        if mask_context1.ndim > 1: # Mask the context (only if necessary)
            e1 = mask_context1 * e1
        alphas_shape1 = e1.shape
        alphas1 = K.softmax(e1.reshape([alphas_shape1[0], alphas_shape1[1]]))
        # sum over the in_timesteps dimension resulting in [batch_size, input_dim]
        ctx_1 = (context1 * alphas1[:, :, None]).sum(axis=1)

        if self.attend_on_both:
            if mask_context2.ndim > 1:  # Mask the context2 (only if necessary)
                pctx_2 = mask_context2[:, :, None] * pctx_2
                context2 = mask_context2[:, :, None] * context2
            if mask_context3.ndim > 1:  # Mask the context2 (only if necessary)
                pctx_3 = mask_context3[:, :, None] * pctx_3
                context3 = mask_context3[:, :, None] * context3

        if self.attend_on_both:
            # Attention model 2 (see Formulation in class header)
            p_state_2 = K.dot(h_tm1 * B_Wa2[0], self.Wa2)
            pctx_2 = K.tanh(pctx_2 + p_state_2[:, None, :])
            e2 = K.dot(pctx_2 * B_wa2[0], self.wa2) + self.ca2
            if mask_context2.ndim > 1: # Mask the context (only if necessary)
                e2 = mask_context2 * e2
            alphas_shape2 = e2.shape
            alphas2 = K.softmax(e2.reshape([alphas_shape2[0], alphas_shape2[1]]))
            # sum over the in_timesteps dimension resulting in [batch_size, input_dim]
            ctx_2 = (context2 * alphas2[:, :, None]).sum(axis=1)

            # Attention model 3 (see Formulation in class header)
            p_state_3 = K.dot(h_tm1 * B_Wa3[0], self.Wa3)
            pctx_3 = K.tanh(pctx_3 + p_state_3[:, None, :])
            e3 = K.dot(pctx_3 * B_wa3[0], self.wa3) + self.ca3
            if mask_context3.ndim > 1: # Mask the context (only if necessary)
                e3 = mask_context3 * e3
            alphas_shape3 = e3.shape
            alphas3 = K.softmax(e3.reshape([alphas_shape3[0], alphas_shape3[1]]))
            # sum over the in_timesteps dimension resulting in [batch_size, input_dim]
            ctx_3 = (context3 * alphas3[:, :, None]).sum(axis=1)
        else:
            ctx_2 = context2
            alphas2 = mask_context2
            ctx_3 = context3
            alphas3 = mask_context3

        z = x + \
            K.dot(h_tm1 * B_U[0], self.U)  + \
            K.dot(ctx_1 * B_T[0], self.T) + \
            K.dot(ctx_2 * B_W[0], self.W) + \
            K.dot(ctx_3 * B_S[0], self.S) + \
            self.b
        z0 = z[:, :self.output_dim]
        z1 = z[:, self.output_dim: 2 * self.output_dim]
        z2 = z[:, 2 * self.output_dim: 3 * self.output_dim]
        z3 = z[:, 3 * self.output_dim:]

        i = self.inner_activation(z0)
        f = self.inner_activation(z1)
        c = f * c_tm1 + i * self.activation(z2)
        o = self.inner_activation(z3)
        h = o * self.activation(c)

        return h, [h, c, ctx_1, alphas1, ctx_2, alphas2, ctx_3, alphas3]

    def get_constants(self, x, mask_context1, mask_context2, mask_context3):
        constants = []
        # States[8]
        if 0 < self.dropout_U < 1:
            ones = K.ones_like(K.reshape(x[:, 0, 0], (-1, 1)))
            ones = K.concatenate([ones] * self.output_dim, 1)
            B_U = [K.in_train_phase(K.dropout(ones, self.dropout_U), ones) for _ in range(4)]
            constants.append(B_U)
        else:
            constants.append([K.cast_to_floatx(1.) for _ in range(4)])

        # States[9]
        if 0 < self.dropout_T < 1:
            input_shape = self.input_spec[1][0].shape
            input_dim = input_shape[-1]
            ones = K.ones_like(K.reshape(x[:, 0, 0], (-1, 1)))
            ones = K.concatenate([ones] * input_dim, 1)
            B_T = [K.in_train_phase(K.dropout(ones, self.dropout_T), ones) for _ in range(4)]
            constants.append(B_T)
        else:
            B_T = [K.cast_to_floatx(1.) for _ in range(4)]
        constants.append(B_T)

        # States[10]
        if 0 < self.dropout_W < 1:
            input_shape = self.input_spec[2][0].shape
            input_dim = input_shape[-1]
            ones = K.ones_like(K.reshape(x[:, 0, 0], (-1, 1)))
            ones = K.concatenate([ones] * input_dim, 1)
            B_W = [K.in_train_phase(K.dropout(ones, self.dropout_W), ones) for _ in range(4)]
            constants.append(B_W)
        else:
            B_W = [K.cast_to_floatx(1.) for _ in range(4)]
        constants.append(B_W)


        # States[11]
        if 0 < self.dropout_S < 1:
            input_shape = self.input_spec[3][0].shape
            input_dim = input_shape[-1]
            ones = K.ones_like(K.reshape(x[:, 0, 0], (-1, 1)))
            ones = K.concatenate([ones] * input_dim, 1)
            B_S = [K.in_train_phase(K.dropout(ones, self.dropout_S), ones) for _ in range(4)]
            constants.append(B_S)
        else:
            B_S = [K.cast_to_floatx(1.) for _ in range(4)]
        constants.append(B_S)


        # AttModel
        # States[12]
        if 0 < self.dropout_wa < 1:
            ones = K.ones_like(K.reshape(self.context1[:, :, 0], (-1, self.context1.shape[1], 1)))
            #ones = K.concatenate([ones], 1)
            B_wa = [K.in_train_phase(K.dropout(ones, self.dropout_wa), ones)]
            constants.append(B_wa)
        else:
            constants.append([K.cast_to_floatx(1.)])

        # States[13]
        if 0 < self.dropout_Wa < 1:
            input_dim = self.output_dim
            ones = K.ones_like(K.reshape(x[:, 0, 0], (-1, 1)))
            ones = K.concatenate([ones] * input_dim, 1)
            B_Wa = [K.in_train_phase(K.dropout(ones, self.dropout_Wa), ones)]
            constants.append(B_Wa)
        else:
            constants.append([K.cast_to_floatx(1.)])

        if self.attend_on_both:
            # AttModel2
            # States[14]
            if 0 < self.dropout_wa2 < 1:
                ones = K.ones_like(K.reshape(self.context2[:, :, 0], (-1, self.context2.shape[1], 1)))
                #ones = K.concatenate([ones], 1)
                B_wa2 = [K.in_train_phase(K.dropout(ones, self.dropout_wa2), ones)]
                constants.append(B_wa2)
            else:
                constants.append([K.cast_to_floatx(1.)])

            # States[15]
            if 0 < self.dropout_Wa2 < 1:
                input_dim = self.output_dim
                ones = K.ones_like(K.reshape(x[:, 0, 0], (-1, 1)))
                ones = K.concatenate([ones] * input_dim, 1)
                B_Wa2 = [K.in_train_phase(K.dropout(ones, self.dropout_Wa2), ones)]
                constants.append(B_Wa2)
            else:
                constants.append([K.cast_to_floatx(1.)])

            # States[16]
            if 0 < self.dropout_wa3 < 1:
                ones = K.ones_like(K.reshape(self.context2[:, :, 0], (-1, self.context3.shape[1], 1)))
                B_wa3 = [K.in_train_phase(K.dropout(ones, self.dropout_wa3), ones)]
                constants.append(B_wa3)
            else:
                constants.append([K.cast_to_floatx(1.)])

            # States[17]
            if 0 < self.dropout_Wa3 < 1:
                input_dim = self.output_dim
                ones = K.ones_like(K.reshape(x[:, 0, 0], (-1, 1)))
                ones = K.concatenate([ones] * input_dim, 1)
                B_Wa3 = [K.in_train_phase(K.dropout(ones, self.dropout_Wa3), ones)]
                constants.append(B_Wa3)
            else:
                constants.append([K.cast_to_floatx(1.)])

        # States[18] - [14]
        constants.append(self.context1)
        # States [19] - [15]
        if mask_context1 is None:
            mask_context1 = K.not_equal(K.sum(self.context1, axis=2), self.mask_value)
        constants.append(mask_context1)

        # States [20] - [15]
        if 0 < self.dropout_Ua < 1:
            input_dim = self.context1_dim
            ones = K.ones_like(K.reshape(self.context1[:, :, 0], (-1, self.context1.shape[1], 1)))
            ones = K.concatenate([ones] * input_dim, axis=2)
            B_Ua = [K.in_train_phase(K.dropout(ones, self.dropout_Ua), ones)]
            pctx1 = K.dot(self.context1 * B_Ua[0], self.Ua) + self.ba
        else:
            pctx1 = K.dot(self.context1, self.Ua) + self.ba
        constants.append(pctx1)

        # States[21] - [16]
        constants.append(self.context2)
        # States [22] - [17]
        if self.attend_on_both:
            if mask_context2 is None:
                mask_context2 = K.not_equal(K.sum(self.context2, axis=2), self.mask_value)
        else:
            mask_context2 = K.ones_like(self.context2[:, 0])
        constants.append(mask_context2)

        # States [23] - [18]
        if self.attend_on_both:
            if 0 < self.dropout_Ua2 < 1:
                input_dim = self.context2_dim
                ones = K.ones_like(K.reshape(self.context2[:, :, 0], (-1, self.context2.shape[1], 1)))
                ones = K.concatenate([ones] * input_dim, axis=2)
                B_Ua2 = [K.in_train_phase(K.dropout(ones, self.dropout_Ua2), ones)]
                pctx2 = K.dot(self.context2 * B_Ua2[0], self.Ua2) + self.ba2
            else:
                pctx2 = K.dot(self.context2, self.Ua2) + self.ba2
            constants.append(pctx2)


        # States[24] - [19]
        constants.append(self.context3)
        # States [25] - [20]
        if self.attend_on_both:
            if mask_context3 is None:
                mask_context3 = K.not_equal(K.sum(self.context3, axis=2), self.mask_value)
        else:
            mask_context3 = K.ones_like(self.context3[:, 0])
        constants.append(mask_context3)

        # States [26] - [21]
        if self.attend_on_both:
            if 0 < self.dropout_Ua3 < 1:
                input_dim = self.context3_dim
                ones = K.ones_like(K.reshape(self.context3[:, :, 0], (-1, self.context3.shape[1], 1)))
                ones = K.concatenate([ones] * input_dim, axis=2)
                B_Ua3 = [K.in_train_phase(K.dropout(ones, self.dropout_Ua3), ones)]
                pctx3 = K.dot(self.context3 * B_Ua3[0], self.Ua3) + self.ba3
            else:
                pctx3 = K.dot(self.context3, self.Ua3) + self.ba3
            constants.append(pctx3)

        if 0 < self.dropout_V < 1:
            input_dim = self.input_dim
            ones = K.ones_like(K.reshape(x[:, :, 0], (-1, x.shape[1], 1)))
            ones = K.concatenate([ones] * input_dim, axis=2)
            B_V = [K.in_train_phase(K.dropout(ones, self.dropout_V), ones) for _ in range(4)]
        else:
            B_V = [K.cast_to_floatx(1.) for _ in range(4)]
        return constants, B_V

    def get_initial_states(self, x):
        # build an all-zero tensor of shape (samples, output_dim)
        if self.init_state is None:
            # build an all-zero tensor of shape (samples, output_dim)
            initial_state = K.zeros_like(x)  # (samples, timesteps, input_dim)
            initial_state = K.sum(initial_state, axis=(1, 2))  # (samples,)
            initial_state = K.expand_dims(initial_state)  # (samples, 1)
            initial_state = K.tile(initial_state, [1, self.output_dim])  # (samples, output_dim)
            if self.init_memory is None:
                initial_states = [initial_state for _ in range(2)]
            else:
                initial_memory = self.init_memory
                initial_states = [initial_state, initial_memory]
        else:
            initial_state = self.init_state
            if self.init_memory is not None: # We have state and memory
                initial_memory = self.init_memory
                initial_states = [initial_state, initial_memory]
            else:
                initial_states = [initial_state for _ in range(2)]

        # extra states for context1 and context2 and context3
        initial_state1 = K.zeros_like(self.context1)  # (samples, input_timesteps, ctx1_dim)
        initial_state_alphas1 = K.sum(initial_state1, axis=2)  # (samples, input_timesteps)
        initial_state1 = K.sum(initial_state1, axis=1)  # (samples, ctx1_dim)
        extra_states = [initial_state1, initial_state_alphas1]
        initial_state2 = K.zeros_like(self.context2)  # (samples, input_timesteps, ctx2_dim)
        initial_state3 = K.zeros_like(self.context3)  # (samples, input_timesteps, ctx2_dim)

        if self.attend_on_both:  # Reduce on temporal dimension
            initial_state_alphas2 = K.sum(initial_state2, axis=2)  # (samples, input_timesteps)
            initial_state2 = K.sum(initial_state2, axis=1)  # (samples, ctx2_dim)
            initial_state_alphas3 = K.sum(initial_state3, axis=2)  # (samples, input_timesteps)
            initial_state3 = K.sum(initial_state3, axis=1)  # (samples, ctx3_dim)
        else:  # Already reduced
            initial_state_alphas2 = initial_state2 # (samples, ctx2_dim)
            initial_state_alphas3 = initial_state3 # (samples, ctx2_dim)

        extra_states.append(initial_state2)
        extra_states.append(initial_state_alphas2)

        extra_states.append(initial_state3)
        extra_states.append(initial_state_alphas3)
        return initial_states + extra_states


    def get_config(self):
        config = {"output_dim": self.output_dim,
                  "return_extra_variables": self.return_extra_variables,
                  "return_states": self.return_states,
                  "mask_value": self.mask_value,
                  "attend_on_both": self.attend_on_both,
                  "init": self.init.__name__,
                  "inner_init": self.inner_init.__name__,
                  "forget_bias_init": self.forget_bias_init.__name__,
                  "activation": self.activation.__name__,
                  "inner_activation": self.inner_activation.__name__,
                  "S_regularizer": self.S_regularizer.get_config() if self.S_regularizer else None,
                  "T_regularizer": self.T_regularizer.get_config() if self.T_regularizer else None,
                  "W_regularizer": self.W_regularizer.get_config() if self.W_regularizer else None,
                  "V_regularizer": self.V_regularizer.get_config() if self.V_regularizer else None,
                  "U_regularizer": self.U_regularizer.get_config() if self.U_regularizer else None,
                  "b_regularizer": self.b_regularizer.get_config() if self.b_regularizer else None,
                  'wa_regularizer': self.wa_regularizer.get_config() if self.wa_regularizer else None,
                  'Wa_regularizer': self.Wa_regularizer.get_config() if self.Wa_regularizer else None,
                  'Ua_regularizer': self.Ua_regularizer.get_config() if self.Ua_regularizer else None,
                  'ba_regularizer': self.ba_regularizer.get_config() if self.ba_regularizer else None,
                  'ca_regularizer': self.ca_regularizer.get_config() if self.ca_regularizer else None,
                  'wa2_regularizer': self.wa2_regularizer.get_config() if self.attend_on_both and self.wa2_regularizer else None,
                  'Wa2_regularizer': self.Wa2_regularizer.get_config() if self.attend_on_both and self.Wa2_regularizer else None,
                  'Ua2_regularizer': self.Ua2_regularizer.get_config() if self.attend_on_both and self.Ua2_regularizer else None,
                  'ba2_regularizer': self.ba2_regularizer.get_config() if self.attend_on_both and self.ba2_regularizer else None,
                  'ca2_regularizer': self.ca2_regularizer.get_config() if self.attend_on_both and self.ca2_regularizer else None,
                  'wa3_regularizer': self.wa3_regularizer.get_config() if self.attend_on_both and self.wa3_regularizer else None,
                  'Wa3_regularizer': self.Wa3_regularizer.get_config() if self.attend_on_both and self.Wa3_regularizer else None,
                  'Ua3_regularizer': self.Ua3_regularizer.get_config() if self.attend_on_both and self.Ua3_regularizer else None,
                  'ba3_regularizer': self.ba3_regularizer.get_config() if self.attend_on_both and self.ba3_regularizer else None,
                  'ca3_regularizer': self.ca3_regularizer.get_config() if self.attend_on_both and self.ca3_regularizer else None,
                  "dropout_S": self.dropout_S,
                  "dropout_T": self.dropout_T,
                  "dropout_W": self.dropout_W,
                  "dropout_U": self.dropout_U,
                  "dropout_V": self.dropout_V,
                  'dropout_wa': self.dropout_wa,
                  'dropout_Wa': self.dropout_Wa,
                  'dropout_Ua': self.dropout_Ua,
                  'dropout_wa2': self.dropout_wa2 if self.attend_on_both else None,
                  'dropout_Wa2': self.dropout_Wa2 if self.attend_on_both else None,
                  'dropout_Ua2': self.dropout_Ua2 if self.attend_on_both else None,
                  'dropout_wa3': self.dropout_wa3 if self.attend_on_both else None,
                  'dropout_Wa3': self.dropout_Wa3 if self.attend_on_both else None,
                  'dropout_Ua3': self.dropout_Ua3 if self.attend_on_both else None
                  }
        base_config = super(AttLSTMCond3Inputs, self).get_config()
        return dict(list(base_config.items()) + list(config.items()))
