# -*- coding: utf-8 -*-
from __future__ import absolute_import

from theano.tensor.shared_randomstreams import RandomStreams
import theano
from theano import tensor as T

from .. import backend as K
from .. import activations, initializations, regularizers, constraints
from ..engine import Layer, InputSpec
from ..utils.np_utils import conv_output_length, conv_input_length

# imports for backwards namespace compatibility
from .pooling import AveragePooling1D, AveragePooling2D, AveragePooling3D
from .pooling import MaxPooling1D, MaxPooling2D, MaxPooling3D

import numpy as np


class ClassActivationMapping(Layer):
    '''Class Activation Mapping computation used in GAP networks.

    # Arguments
        weights: Set of weights (numpy.array) already learned that connect a
            GAP (global average pooling) layer with a Dense layer.

    # References
        [1]Zhou B, Khosla A, Lapedriza A, Oliva A, Torralba A. 
            Learning Deep Features for Discriminative Localization. 
            arXiv preprint arXiv:1512.04150. 2015 Dec 14.
    '''
    def __init__(self, weights_shape, weights=None, **kwargs):
        self.weights_shape = weights_shape
        self.initial_weights = [weights]
        self.init = initializations.get('uniform', dim_ordering='th')
        self.input_spec = [InputSpec(ndim=4)]
        super(ClassActivationMapping, self).__init__(**kwargs)


    def build(self, input_shape):
        self.W = self.init(self.weights_shape,
                           name='{}_W'.format(self.name))
        self.trainable_weights = [self.W]

        # initialize weights
        if(self.initial_weights[0] is not None):
            self.set_weights(self.initial_weights)

        
    def call(self, x, mask=None):
        '''
        # Formulation
            The original CAM formulation from [1] is as follows:         

                CAM(x,y,c) = ∑_k w_k(c) * f_k(x,y),

            where CAM(x,y,c) is the class activation map of class 'c' 
            at pixel (x,y), w_k is the weight (self.W) of the k-th kernel 
            learned on the Dense layer after GAP, and f_k is the feature 
            activation at pixel (x,y) produced by the deep convolution layers
            applied before the GAP layer.
        '''
        x = K.permute_dimensions(x, (0,2,3,1))
        x = K.dot(x, self.W)
        x = K.permute_dimensions(x, (0,3,1,2)) # (batch_size, n_classes, x, y)
        return x

    def get_output_shape_for(self, input_shape):
        return tuple([input_shape[0]] + [self.weights_shape[1]] + list(input_shape[2:]))

    def get_config(self):
        config = {'weights_shape': self.weights_shape}
        base_config = super(ClassActivationMapping, self).get_config()
        return dict(list(base_config.items()) + list(config.items()))


class Convolution1D(Layer):
    '''Convolution operator for filtering neighborhoods of one-dimensional inputs.
    When using this layer as the first layer in a model,
    either provide the keyword argument `input_dim`
    (int, e.g. 128 for sequences of 128-dimensional vectors),
    or `input_shape` (tuple of integers, e.g. (10, 128) for sequences
    of 10 vectors of 128-dimensional vectors).

    # Example

    ```python
        # apply a convolution 1d of length 3 to a sequence with 10 timesteps,
        # with 64 output filters
        model = Sequential()
        model.add(Convolution1D(64, 3, border_mode='same', input_shape=(10, 32)))
        # now model.output_shape == (None, 10, 64)

        # add a new conv1d on top
        model.add(Convolution1D(32, 3, border_mode='same'))
        # now model.output_shape == (None, 10, 32)
    ```

    # Arguments
        nb_filter: Number of convolution kernels to use
            (dimensionality of the output).
        filter_length: The extension (spatial or temporal) of each filter.
        init: name of initialization function for the weights of the layer
            (see [initializations](../initializations.md)),
            or alternatively, Theano function to use for weights initialization.
            This parameter is only relevant if you don't pass a `weights` argument.
        activation: name of activation function to use
            (see [activations](../activations.md)),
            or alternatively, elementwise Theano function.
            If you don't specify anything, no activation is applied
            (ie. "linear" activation: a(x) = x).
        weights: list of numpy arrays to set as initial weights.
        border_mode: 'valid', 'same' or 'full'. ('full' requires the Theano backend.)
        subsample_length: factor by which to subsample output.
        W_regularizer: instance of [WeightRegularizer](../regularizers.md)
            (eg. L1 or L2 regularization), applied to the main weights matrix.
        b_regularizer: instance of [WeightRegularizer](../regularizers.md),
            applied to the bias.
        activity_regularizer: instance of [ActivityRegularizer](../regularizers.md),
            applied to the network output.
        W_constraint: instance of the [constraints](../constraints.md) module
            (eg. maxnorm, nonneg), applied to the main weights matrix.
        b_constraint: instance of the [constraints](../constraints.md) module,
            applied to the bias.
        bias: whether to include a bias
            (i.e. make the layer affine rather than linear).
        input_dim: Number of channels/dimensions in the input.
            Either this argument or the keyword argument `input_shape`must be
            provided when using this layer as the first layer in a model.
        input_length: Length of input sequences, when it is constant.
            This argument is required if you are going to connect
            `Flatten` then `Dense` layers upstream
            (without it, the shape of the dense outputs cannot be computed).

    # Input shape
        3D tensor with shape: `(samples, steps, input_dim)`.

    # Output shape
        3D tensor with shape: `(samples, new_steps, nb_filter)`.
        `steps` value might have changed due to padding.
    '''
    def __init__(self, nb_filter, filter_length,
                 init='glorot_uniform', activation=None, weights=None,
                 border_mode='valid', subsample_length=1,
                 W_regularizer=None, b_regularizer=None, activity_regularizer=None,
                 W_constraint=None, b_constraint=None,
                 W_learning_rate_multiplier=None, b_learning_rate_multiplier=None,
                 bias=True, input_dim=None, input_length=None, **kwargs):

        if border_mode not in {'valid', 'same', 'full'}:
            raise ValueError('Invalid border mode for Convolution1D:', border_mode)
        self.nb_filter = nb_filter
        self.filter_length = filter_length
        self.init = initializations.get(init, dim_ordering='th')
        self.activation = activations.get(activation)
        self.border_mode = border_mode
        self.subsample_length = subsample_length

        self.subsample = (subsample_length, 1)

        self.W_regularizer = regularizers.get(W_regularizer)
        self.b_regularizer = regularizers.get(b_regularizer)
        self.activity_regularizer = regularizers.get(activity_regularizer)

        self.W_constraint = constraints.get(W_constraint)
        self.b_constraint = constraints.get(b_constraint)

        self.W_learning_rate_multiplier = W_learning_rate_multiplier
        self.b_learning_rate_multiplier = b_learning_rate_multiplier
        self.learning_rate_multipliers = [self.W_learning_rate_multiplier,
                                          self.b_learning_rate_multiplier]

        self.bias = bias
        self.input_spec = [InputSpec(ndim=3)]
        self.initial_weights = weights
        self.input_dim = input_dim
        self.input_length = input_length
        self.supports_masking = True
        if self.input_dim:
            kwargs['input_shape'] = (self.input_length, self.input_dim)
        super(Convolution1D, self).__init__(**kwargs)

    def build(self, input_shape):
        input_dim = input_shape[2]
        self.W_shape = (self.filter_length, 1, input_dim, self.nb_filter)

        self.W = self.add_weight(self.W_shape,
                                 initializer=self.init,
                                 name='{}_W'.format(self.name),
                                 regularizer=self.W_regularizer,
                                 constraint=self.W_constraint)
        if self.bias:
            self.b = self.add_weight((self.nb_filter,),
                                     initializer='zero',
                                     name='{}_b'.format(self.name),
                                     regularizer=self.b_regularizer,
                                     constraint=self.b_constraint)
        else:
            self.b = None

        if self.initial_weights is not None:
            self.set_weights(self.initial_weights)
            del self.initial_weights
        self.built = True

    def get_output_shape_for(self, input_shape):
        length = conv_output_length(input_shape[1],
                                    self.filter_length,
                                    self.border_mode,
                                    self.subsample[0])
        return (input_shape[0], length, self.nb_filter)

    def call(self, x, mask=None):
        x = K.expand_dims(x, 2)  # add a dummy dimension
        output = K.conv2d(x, self.W, strides=self.subsample,
                          border_mode=self.border_mode,
                          dim_ordering='tf')
        output = K.squeeze(output, 2)  # remove the dummy dimension
        if self.bias:
            output += K.reshape(self.b, (1, 1, self.nb_filter))
        output = self.activation(output)
        return output

    def get_config(self):
        config = {'nb_filter': self.nb_filter,
                  'filter_length': self.filter_length,
                  'init': self.init.__name__,
                  'activation': self.activation.__name__,
                  'border_mode': self.border_mode,
                  'subsample_length': self.subsample_length,
                  'W_regularizer': self.W_regularizer.get_config() if self.W_regularizer else None,
                  'b_regularizer': self.b_regularizer.get_config() if self.b_regularizer else None,
                  'activity_regularizer': self.activity_regularizer.get_config() if self.activity_regularizer else None,
                  'W_constraint': self.W_constraint.get_config() if self.W_constraint else None,
                  'b_constraint': self.b_constraint.get_config() if self.b_constraint else None,
                  'b_learning_rate_multiplier': self.b_learning_rate_multiplier,
                  'bias': self.bias,
                  'input_dim': self.input_dim,
                  'input_length': self.input_length}
        base_config = super(Convolution1D, self).get_config()
        return dict(list(base_config.items()) + list(config.items()))

    def set_lr_multipliers(self, W_learning_rate_multiplier, b_learning_rate_multiplier):
        self.W_learning_rate_multiplier = W_learning_rate_multiplier
        self.b_learning_rate_multiplier = b_learning_rate_multiplier
        self.learning_rate_multipliers = [self.W_learning_rate_multiplier,
                                          self.b_learning_rate_multiplier]

class AtrousConvolution1D(Convolution1D):
    '''Atrous Convolution operator for filtering neighborhoods of one-dimensional inputs.
    A.k.a dilated convolution or convolution with holes.
    When using this layer as the first layer in a model,
    either provide the keyword argument `input_dim`
    (int, e.g. 128 for sequences of 128-dimensional vectors),
    or `input_shape` (tuples of integers, e.g. (10, 128) for sequences
    of 10 vectors of 128-dimensional vectors).

    # Example

    ```python
        # apply an atrous convolution 1d with atrous rate 2 of length 3 to a sequence with 10 timesteps,
        # with 64 output filters
        model = Sequential()
        model.add(AtrousConvolution1D(64, 3, atrous_rate=2, border_mode='same', input_shape=(10, 32)))
        # now model.output_shape == (None, 10, 64)

        # add a new atrous conv1d on top
        model.add(AtrousConvolution1D(32, 3, atrous_rate=2, border_mode='same'))
        # now model.output_shape == (None, 10, 32)
    ```

    # Arguments
        nb_filter: Number of convolution kernels to use
            (dimensionality of the output).
        filter_length: The extension (spatial or temporal) of each filter.
        init: name of initialization function for the weights of the layer
            (see [initializations](../initializations.md)),
            or alternatively, Theano function to use for weights initialization.
            This parameter is only relevant if you don't pass a `weights` argument.
        activation: name of activation function to use
            (see [activations](../activations.md)),
            or alternatively, elementwise Theano function.
            If you don't specify anything, no activation is applied
            (ie. "linear" activation: a(x) = x).
        weights: list of numpy arrays to set as initial weights.
        border_mode: 'valid', 'same' or 'full'. ('full' requires the Theano backend.)
        subsample_length: factor by which to subsample output.
        atrous_rate: Factor for kernel dilation. Also called filter_dilation
            elsewhere.
        W_regularizer: instance of [WeightRegularizer](../regularizers.md)
            (eg. L1 or L2 regularization), applied to the main weights matrix.
        b_regularizer: instance of [WeightRegularizer](../regularizers.md),
            applied to the bias.
        activity_regularizer: instance of [ActivityRegularizer](../regularizers.md),
            applied to the network output.
        W_constraint: instance of the [constraints](../constraints.md) module
            (eg. maxnorm, nonneg), applied to the main weights matrix.
        b_constraint: instance of the [constraints](../constraints.md) module,
            applied to the bias.
        bias: whether to include a bias
            (i.e. make the layer affine rather than linear).
        input_dim: Number of channels/dimensions in the input.
            Either this argument or the keyword argument `input_shape`must be
            provided when using this layer as the first layer in a model.
        input_length: Length of input sequences, when it is constant.
            This argument is required if you are going to connect
            `Flatten` then `Dense` layers upstream
            (without it, the shape of the dense outputs cannot be computed).

    # Input shape
        3D tensor with shape: `(samples, steps, input_dim)`.

    # Output shape
        3D tensor with shape: `(samples, new_steps, nb_filter)`.
        `steps` value might have changed due to padding.
    '''
    def __init__(self, nb_filter, filter_length,
                 init='glorot_uniform', activation=None, weights=None,
                 border_mode='valid', subsample_length=1, atrous_rate=1,
                 W_regularizer=None, b_regularizer=None, activity_regularizer=None,
                 W_constraint=None, b_constraint=None,
                 bias=True, **kwargs):

        if border_mode not in {'valid', 'same', 'full'}:
            raise ValueError('Invalid border mode for AtrousConv1D:', border_mode)

        self.atrous_rate = int(atrous_rate)

        super(AtrousConvolution1D, self).__init__(nb_filter, filter_length,
                                                  init=init, activation=activation,
                                                  weights=weights, border_mode=border_mode,
                                                  subsample_length=subsample_length,
                                                  W_regularizer=W_regularizer, b_regularizer=b_regularizer,
                                                  activity_regularizer=activity_regularizer,
                                                  W_constraint=W_constraint, b_constraint=b_constraint,
                                                  bias=bias, **kwargs)

    def get_output_shape_for(self, input_shape):
        length = conv_output_length(input_shape[1],
                                    self.filter_length,
                                    self.border_mode,
                                    self.subsample[0],
                                    dilation=self.atrous_rate)
        return (input_shape[0], length, self.nb_filter)

    def call(self, x, mask=None):
        x = K.expand_dims(x, 2)  # add a dummy dimension
        output = K.conv2d(x, self.W, strides=self.subsample,
                          border_mode=self.border_mode,
                          dim_ordering='tf',
                          filter_dilation=(self.atrous_rate, self.atrous_rate))
        output = K.squeeze(output, 2)  # remove the dummy dimension
        if self.bias:
            output += K.reshape(self.b, (1, 1, self.nb_filter))
        output = self.activation(output)
        return output

    def get_config(self):
        config = {'atrous_rate': self.atrous_rate}
        base_config = super(AtrousConvolution1D, self).get_config()
        return dict(list(base_config.items()) + list(config.items()))


class Convolution2D(Layer):
    '''Convolution operator for filtering windows of two-dimensional inputs.
    When using this layer as the first layer in a model,
    provide the keyword argument `input_shape`
    (tuple of integers, does not include the sample axis),
    e.g. `input_shape=(3, 128, 128)` for 128x128 RGB pictures.

    # Examples

    ```python
        # apply a 3x3 convolution with 64 output filters on a 256x256 image:
        model = Sequential()
        model.add(Convolution2D(64, 3, 3, border_mode='same', input_shape=(3, 256, 256)))
        # now model.output_shape == (None, 64, 256, 256)

        # add a 3x3 convolution on top, with 32 output filters:
        model.add(Convolution2D(32, 3, 3, border_mode='same'))
        # now model.output_shape == (None, 32, 256, 256)
    ```

    # Arguments
        nb_filter: Number of convolution filters to use.
        nb_row: Number of rows in the convolution kernel.
        nb_col: Number of columns in the convolution kernel.
        init: name of initialization function for the weights of the layer
            (see [initializations](../initializations.md)), or alternatively,
            Theano function to use for weights initialization.
            This parameter is only relevant if you don't pass
            a `weights` argument.
        activation: name of activation function to use
            (see [activations](../activations.md)),
            or alternatively, elementwise Theano function.
            If you don't specify anything, no activation is applied
            (ie. "linear" activation: a(x) = x).
        weights: list of numpy arrays to set as initial weights.
        border_mode: 'valid', 'same' or 'full'. ('full' requires the Theano backend.)
        subsample: tuple of length 2. Factor by which to subsample output.
            Also called strides elsewhere.
        W_regularizer: instance of [WeightRegularizer](../regularizers.md)
            (eg. L1 or L2 regularization), applied to the main weights matrix.
        b_regularizer: instance of [WeightRegularizer](../regularizers.md),
            applied to the bias.
        activity_regularizer: instance of [ActivityRegularizer](../regularizers.md),
            applied to the network output.
        W_constraint: instance of the [constraints](../constraints.md) module
            (eg. maxnorm, nonneg), applied to the main weights matrix.
        b_constraint: instance of the [constraints](../constraints.md) module,
            applied to the bias.
        dim_ordering: 'th' or 'tf'. In 'th' mode, the channels dimension
            (the depth) is at index 1, in 'tf' mode is it at index 3.
            It defaults to the `image_dim_ordering` value found in your
            Keras config file at `~/.keras/keras.json`.
            If you never set it, then it will be "tf".
        bias: whether to include a bias
            (i.e. make the layer affine rather than linear).

    # Input shape
        4D tensor with shape:
        `(samples, channels, rows, cols)` if dim_ordering='th'
        or 4D tensor with shape:
        `(samples, rows, cols, channels)` if dim_ordering='tf'.

    # Output shape
        4D tensor with shape:
        `(samples, nb_filter, new_rows, new_cols)` if dim_ordering='th'
        or 4D tensor with shape:
        `(samples, new_rows, new_cols, nb_filter)` if dim_ordering='tf'.
        `rows` and `cols` values might have changed due to padding.
    '''
    def __init__(self, nb_filter, nb_row, nb_col,
                 init='glorot_uniform', activation=None, weights=None,
                 border_mode='valid', subsample=(1, 1), dim_ordering='default',
                 W_regularizer=None, b_regularizer=None, activity_regularizer=None,
                 W_constraint=None, b_constraint=None, 
                 W_learning_rate_multiplier=None, b_learning_rate_multiplier=None,
                 bias=True, **kwargs):
        if dim_ordering == 'default':
            dim_ordering = K.image_dim_ordering()
        if border_mode not in {'valid', 'same', 'full'}:
            raise ValueError('Invalid border mode for Convolution2D:', border_mode)
        self.nb_filter = nb_filter
        self.nb_row = nb_row
        self.nb_col = nb_col
        self.init = initializations.get(init, dim_ordering=dim_ordering)
        self.activation = activations.get(activation)
        self.border_mode = border_mode
        self.subsample = tuple(subsample)
        if dim_ordering not in {'tf', 'th'}:
            raise ValueError('dim_ordering must be in {tf, th}.')
        self.dim_ordering = dim_ordering

        self.W_regularizer = regularizers.get(W_regularizer)
        self.b_regularizer = regularizers.get(b_regularizer)
        self.activity_regularizer = regularizers.get(activity_regularizer)

        self.W_constraint = constraints.get(W_constraint)
        self.b_constraint = constraints.get(b_constraint)

        self.W_learning_rate_multiplier = W_learning_rate_multiplier
        self.b_learning_rate_multiplier = b_learning_rate_multiplier
        self.learning_rate_multipliers = [self.W_learning_rate_multiplier, self.b_learning_rate_multiplier]

        self.bias = bias
        self.input_spec = [InputSpec(ndim=4)]
        self.initial_weights = weights
        self.supports_masking = True
        super(Convolution2D, self).__init__(**kwargs)

    def build(self, input_shape):
        if self.dim_ordering == 'th':
            stack_size = input_shape[1]
            self.W_shape = (self.nb_filter, stack_size, self.nb_row, self.nb_col)
        elif self.dim_ordering == 'tf':
            stack_size = input_shape[3]
            self.W_shape = (self.nb_row, self.nb_col, stack_size, self.nb_filter)
        else:
            raise ValueError('Invalid dim_ordering:', self.dim_ordering)
        self.W = self.add_weight(self.W_shape,
                                 initializer=self.init,
                                 name='{}_W'.format(self.name),
                                 regularizer=self.W_regularizer,
                                 constraint=self.W_constraint)
        if self.bias:
            self.b = self.add_weight((self.nb_filter,),
                                     initializer='zero',
                                     name='{}_b'.format(self.name),
                                     regularizer=self.b_regularizer,
                                     constraint=self.b_constraint)
        else:
            self.b = None

        if self.initial_weights is not None:
            self.set_weights(self.initial_weights)
            del self.initial_weights
        self.built = True

    def get_output_shape_for(self, input_shape):
        if self.dim_ordering == 'th':
            rows = input_shape[2]
            cols = input_shape[3]
        elif self.dim_ordering == 'tf':
            rows = input_shape[1]
            cols = input_shape[2]
        else:
            raise ValueError('Invalid dim_ordering:', self.dim_ordering)

        rows = conv_output_length(rows, self.nb_row,
                                  self.border_mode, self.subsample[0])
        cols = conv_output_length(cols, self.nb_col,
                                  self.border_mode, self.subsample[1])

        if self.dim_ordering == 'th':
            return (input_shape[0], self.nb_filter, rows, cols)
        elif self.dim_ordering == 'tf':
            return (input_shape[0], rows, cols, self.nb_filter)

    def call(self, x, mask=None):
        output = K.conv2d(x, self.W, strides=self.subsample,
                          border_mode=self.border_mode,
                          dim_ordering=self.dim_ordering,
                          filter_shape=self.W_shape)
        if self.bias:
            if self.dim_ordering == 'th':
                output += K.reshape(self.b, (1, self.nb_filter, 1, 1))
            elif self.dim_ordering == 'tf':
                output += K.reshape(self.b, (1, 1, 1, self.nb_filter))
            else:
                raise ValueError('Invalid dim_ordering:', self.dim_ordering)
        output = self.activation(output)
        return output

    def get_config(self):
        config = {'nb_filter': self.nb_filter,
                  'nb_row': self.nb_row,
                  'nb_col': self.nb_col,
                  'init': self.init.__name__,
                  'activation': self.activation.__name__,
                  'border_mode': self.border_mode,
                  'subsample': self.subsample,
                  'dim_ordering': self.dim_ordering,
                  'W_regularizer': self.W_regularizer.get_config() if self.W_regularizer else None,
                  'b_regularizer': self.b_regularizer.get_config() if self.b_regularizer else None,
                  'activity_regularizer': self.activity_regularizer.get_config() if self.activity_regularizer else None,
                  'W_constraint': self.W_constraint.get_config() if self.W_constraint else None,
                  'b_constraint': self.b_constraint.get_config() if self.b_constraint else None,
                  'W_learning_rate_multiplier': self.W_learning_rate_multiplier,
                  'b_learning_rate_multiplier': self.b_learning_rate_multiplier,
                  'bias': self.bias}
        base_config = super(Convolution2D, self).get_config()
        return dict(list(base_config.items()) + list(config.items()))

    def set_lr_multipliers(self, W_learning_rate_multiplier, b_learning_rate_multiplier):
        self.W_learning_rate_multiplier = W_learning_rate_multiplier
        self.b_learning_rate_multiplier = b_learning_rate_multiplier
        self.learning_rate_multipliers = [self.W_learning_rate_multiplier,
                                          self.b_learning_rate_multiplier]


class Deconvolution2D(Convolution2D):
    '''Transposed convolution operator for filtering windows of two-dimensional inputs.
    The need for transposed convolutions generally arises from the desire
    to use a transformation going in the opposite direction of a normal convolution,
    i.e., from something that has the shape of the output of some convolution
    to something that has the shape of its input
    while maintaining a connectivity pattern that is compatible with said convolution. [1]

    When using this layer as the first layer in a model,
    provide the keyword argument `input_shape`
    (tuple of integers, does not include the sample axis),
    e.g. `input_shape=(3, 128, 128)` for 128x128 RGB pictures.

    To pass the correct `output_shape` to this layer,
    one could use a test model to predict and observe the actual output shape.

    # Examples

    ```python
        # apply a 3x3 transposed convolution with stride 1x1 and 3 output filters on a 12x12 image:
        model = Sequential()
        model.add(Deconvolution2D(3, 3, 3, output_shape=(None, 3, 14, 14), border_mode='valid', input_shape=(3, 12, 12)))
        # Note that you will have to change the output_shape depending on the backend used.

        # we can predict with the model and print the shape of the array.
        dummy_input = np.ones((32, 3, 12, 12))
        # For TensorFlow dummy_input = np.ones((32, 12, 12, 3))
        preds = model.predict(dummy_input)
        print(preds.shape)
        # Theano GPU: (None, 3, 13, 13)
        # Theano CPU: (None, 3, 14, 14)
        # TensorFlow: (None, 14, 14, 3)

        # apply a 3x3 transposed convolution with stride 2x2 and 3 output filters on a 12x12 image:
        model = Sequential()
        model.add(Deconvolution2D(3, 3, 3, output_shape=(None, 3, 25, 25), subsample=(2, 2), border_mode='valid', input_shape=(3, 12, 12)))
        model.summary()

        # we can predict with the model and print the shape of the array.
        dummy_input = np.ones((32, 3, 12, 12))
        # For TensorFlow dummy_input = np.ones((32, 12, 12, 3))
        preds = model.predict(dummy_input)
        print(preds.shape)
        # Theano GPU: (None, 3, 25, 25)
        # Theano CPU: (None, 3, 25, 25)
        # TensorFlow: (None, 25, 25, 3)
    ```

    # Arguments
        nb_filter: Number of transposed convolution filters to use.
        nb_row: Number of rows in the transposed convolution kernel.
        nb_col: Number of columns in the transposed convolution kernel.
        output_shape: Output shape of the transposed convolution operation.
            tuple of integers (nb_samples, nb_filter, nb_output_rows, nb_output_cols)
            Formula for calculation of the output shape [1], [2]:
                o = s (i - 1) + a + k - 2p, \quad a \in \{0, \ldots, s - 1\}
                where:
                    i - input size (rows or cols),
                    k - kernel size (nb_filter),
                    s - stride (subsample for rows or cols respectively),
                    p - padding size,
                    a - user-specified quantity used to distinguish between
                        the s different possible output sizes.
             Because a is not specified explicitly and Theano and Tensorflow
             use different values, it is better to use a dummy input and observe
             the actual output shape of a layer as specified in the examples.
        init: name of initialization function for the weights of the layer
            (see [initializations](../initializations.md)), or alternatively,
            Theano function to use for weights initialization.
            This parameter is only relevant if you don't pass
            a `weights` argument.
        activation: name of activation function to use
            (see [activations](../activations.md)),
            or alternatively, elementwise Theano/TensorFlow function.
            If you don't specify anything, no activation is applied
            (ie. "linear" activation: a(x) = x).
        weights: list of numpy arrays to set as initial weights.
        border_mode: 'valid', 'same' or 'full'. ('full' requires the Theano backend.)
        subsample: tuple of length 2. Factor by which to oversample output.
            Also called strides elsewhere.
        W_regularizer: instance of [WeightRegularizer](../regularizers.md)
            (eg. L1 or L2 regularization), applied to the main weights matrix.
        b_regularizer: instance of [WeightRegularizer](../regularizers.md),
            applied to the bias.
        activity_regularizer: instance of [ActivityRegularizer](../regularizers.md),
            applied to the network output.
        W_constraint: instance of the [constraints](../constraints.md) module
            (eg. maxnorm, nonneg), applied to the main weights matrix.
        b_constraint: instance of the [constraints](../constraints.md) module,
            applied to the bias.
        dim_ordering: 'th' or 'tf'. In 'th' mode, the channels dimension
            (the depth) is at index 1, in 'tf' mode is it at index 3.
            It defaults to the `image_dim_ordering` value found in your
            Keras config file at `~/.keras/keras.json`.
            If you never set it, then it will be "tf".
        bias: whether to include a bias (i.e. make the layer affine rather than linear).

    # Input shape
        4D tensor with shape:
        `(samples, channels, rows, cols)` if dim_ordering='th'
        or 4D tensor with shape:
        `(samples, rows, cols, channels)` if dim_ordering='tf'.

    # Output shape
        4D tensor with shape:
        `(samples, nb_filter, new_rows, new_cols)` if dim_ordering='th'
        or 4D tensor with shape:
        `(samples, new_rows, new_cols, nb_filter)` if dim_ordering='tf'.
        `rows` and `cols` values might have changed due to padding.

    # References
        [1] [A guide to convolution arithmetic for deep learning](https://arxiv.org/abs/1603.07285 "arXiv:1603.07285v1 [stat.ML]")
        [2] [Transposed convolution arithmetic](http://deeplearning.net/software/theano_versions/dev/tutorial/conv_arithmetic.html#transposed-convolution-arithmetic)
        [3] [Deconvolutional Networks](http://www.matthewzeiler.com/pubs/cvpr2010/cvpr2010.pdf)
    '''
    def __init__(self, nb_filter, nb_row, nb_col, output_shape=None,
                 init='glorot_uniform', activation=None, weights=None,
                 border_mode='valid', subsample=(1, 1),
                 dim_ordering='default',
                 W_regularizer=None, b_regularizer=None, activity_regularizer=None,
                 W_constraint=None, b_constraint=None, 
                 W_learning_rate_multiplier=None, b_learning_rate_multiplier=None,
                 bias=True, **kwargs):
        if dim_ordering == 'default':
            dim_ordering = K.image_dim_ordering()
        if border_mode not in {'valid', 'same', 'full'}:
            raise ValueError('Invalid border mode for Deconvolution2D:', border_mode)

        if output_shape is None:
            output_shape = (None, nb_filter, None, None)
        self.output_shape_ = output_shape

        super(Deconvolution2D, self).__init__(nb_filter, nb_row, nb_col,
                                              init=init,
                                              activation=activation,
                                              weights=weights,
                                              border_mode=border_mode,
                                              subsample=subsample,
                                              dim_ordering=dim_ordering,
                                              W_regularizer=W_regularizer,
                                              b_regularizer=b_regularizer,
                                              activity_regularizer=activity_regularizer,
                                              W_constraint=W_constraint,
                                              b_constraint=b_constraint,
                                              bias=bias,
                                              **kwargs)

    def get_output_shape_for(self, input_shape):
        if self.dim_ordering == 'th':
            rows = self.output_shape_[2]
            cols = self.output_shape_[3]
        elif self.dim_ordering == 'tf':
            rows = self.output_shape_[1]
            cols = self.output_shape_[2]
        else:
            raise ValueError('Invalid dim_ordering:', self.dim_ordering)

        if self.dim_ordering == 'th':
            return (input_shape[0], self.nb_filter, rows, cols)
        elif self.dim_ordering == 'tf':
            return (input_shape[0], rows, cols, self.nb_filter)

    def call(self, x, mask=None):
        output = K.deconv2d(x, self.W, self.output_shape_,
                            strides=self.subsample,
                            border_mode=self.border_mode,
                            dim_ordering=self.dim_ordering,
                            filter_shape=self.W_shape)
        if self.bias:
            if self.dim_ordering == 'th':
                output += K.reshape(self.b, (1, self.nb_filter, 1, 1))
            elif self.dim_ordering == 'tf':
                output += K.reshape(self.b, (1, 1, 1, self.nb_filter))
            else:
                raise ValueError('Invalid dim_ordering:', self.dim_ordering)
        output = self.activation(output)
        return output

    def get_config(self):
        config = {'output_shape': self.output_shape_}
        base_config = super(Deconvolution2D, self).get_config()
        return dict(list(base_config.items()) + list(config.items()))

    def set_lr_multipliers(self, W_learning_rate_multiplier, b_learning_rate_multiplier):
        self.W_learning_rate_multiplier = W_learning_rate_multiplier
        self.b_learning_rate_multiplier = b_learning_rate_multiplier
        self.learning_rate_multipliers = [self.W_learning_rate_multiplier,
                                          self.b_learning_rate_multiplier]

class ArbitraryDeconvolution2D(Convolution2D):
    '''Transposed convolution operator for filtering windows of two-dimensional inputs of arbitrary size.

    # Arguments
        nb_filter: Number of transposed convolution filters to use.
        nb_row: Number of rows in the transposed convolution kernel.
        nb_col: Number of columns in the transposed convolution kernel.
        init: name of initialization function for the weights of the layer
            (see [initializations](../initializations.md)), or alternatively,
            Theano function to use for weights initialization.
            This parameter is only relevant if you don't pass
            a `weights` argument.
        activation: name of activation function to use
            (see [activations](../activations.md)),
            or alternatively, elementwise Theano/TensorFlow function.
            If you don't specify anything, no activation is applied
            (ie. "linear" activation: a(x) = x).
        weights: list of numpy arrays to set as initial weights.
        border_mode: 'valid', 'same' or 'full'. ('full' requires the Theano backend.)
        subsample: tuple of length 2. Factor by which to oversample output.
            Also called strides elsewhere.
        dim_ordering: 'th' or 'tf'. In 'th' mode, the channels dimension
            (the depth) is at index 1, in 'tf' mode is it at index 3.
            It defaults to the `image_dim_ordering` value found in your
            Keras config file at `~/.keras/keras.json`.
            If you never set it, then it will be "tf".
        W_regularizer: instance of [WeightRegularizer](../regularizers.md)
            (eg. L1 or L2 regularization), applied to the main weights matrix.
        b_regularizer: instance of [WeightRegularizer](../regularizers.md),
            applied to the bias.
        activity_regularizer: instance of [ActivityRegularizer](../regularizers.md),
            applied to the network output.
        W_constraint: instance of the [constraints](../constraints.md) module
            (eg. maxnorm, nonneg), applied to the main weights matrix.
        b_constraint: instance of the [constraints](../constraints.md) module,
            applied to the bias.
        bias: whether to include a bias (i.e. make the layer affine rather than linear).
    '''
    def __init__(self, nb_filter, nb_row, nb_col, input_shape,
                 init='glorot_uniform', activation=None, weights=None,
                 border_mode='valid', subsample=(1, 1),
                 dim_ordering='default',
                 W_regularizer=None, b_regularizer=None, activity_regularizer=None,
                 W_constraint=None, b_constraint=None,
                 bias=True, **kwargs):

        if dim_ordering == 'default':
            dim_ordering = K.image_dim_ordering()
        if border_mode not in {'valid', 'same'}:
            raise ValueError('Invalid border mode for ArbitraryDeconvolution2D:', border_mode)

        self.output_shape_ = self.get_output_shape_for_helper(input_shape, nb_filter,
                                                              dim_ordering, nb_row, nb_col,
                                                              border_mode, subsample)
        self.input_shape_ = input_shape

        super(ArbitraryDeconvolution2D, self).__init__(nb_filter, nb_row, nb_col,
                                              init=init,
                                              activation=activation,
                                              weights=weights,
                                              border_mode=border_mode,
                                              subsample=subsample,
                                              dim_ordering=dim_ordering,
                                              W_regularizer=W_regularizer,
                                              b_regularizer=b_regularizer,
                                              activity_regularizer=activity_regularizer,
                                              W_constraint=W_constraint,
                                              b_constraint=b_constraint,
                                              bias=bias,
                                              **kwargs)

    def get_output_shape_for_helper(self, input_shape,
                                    nb_filter, dim_ordering,
                                    nb_row, nb_col,
                                    border_mode, subsample):
        if dim_ordering == 'th':
            rows = input_shape[2]
            cols = input_shape[3]
        elif dim_ordering == 'tf':
            rows = input_shape[1]
            cols = input_shape[2]
        else:
            raise Exception('Invalid dim_ordering: ' + dim_ordering)

        rows = conv_input_length(rows, nb_row,
                                 border_mode, subsample[0])
        cols = conv_input_length(cols, nb_col,
                                 border_mode, subsample[1])

        if dim_ordering == 'th':
            return (input_shape[0], nb_filter, rows, cols)
        elif dim_ordering == 'tf':
            return (input_shape[0], rows, cols, nb_filter)

    def get_output_shape_for(self, input_shape):
        if self.dim_ordering == 'th':
            rows = input_shape[2]
            cols = input_shape[3]
        elif self.dim_ordering == 'tf':
            rows = self.output_shape_[1]
            cols = self.output_shape_[2]
        else:
            raise Exception('Invalid dim_ordering:', self.dim_ordering)

        if self.dim_ordering == 'th':
            return (input_shape[0], self.nb_filter, rows, cols)
        elif self.dim_ordering == 'tf':
            return (input_shape[0], rows, cols, self.nb_filter)

    def call(self, x, mask=None):
        output = K.deconv2d(x, self.W, self.output_shape_,
                            strides=self.subsample,
                            border_mode=self.border_mode,
                            dim_ordering=self.dim_ordering,
                            filter_shape=self.W_shape)

        if self.bias:
            if self.dim_ordering == 'th':
                output += K.reshape(self.b, (1, self.nb_filter, 1, 1))
            elif self.dim_ordering == 'tf':
                output += K.reshape(self.b, (1, 1, 1, self.nb_filter))
            else:
                raise Exception('Invalid dim_ordering:', self.dim_ordering)
        output = self.activation(output)
        return output

    def get_config(self):
        config = {'input_shape': self.input_shape_}
        base_config = super(ArbitraryDeconvolution2D, self).get_config()
        return dict(list(base_config.items()) + list(config.items()))

    def set_lr_multipliers(self, W_learning_rate_multiplier, b_learning_rate_multiplier):
        self.W_learning_rate_multiplier = W_learning_rate_multiplier
        self.b_learning_rate_multiplier = b_learning_rate_multiplier
        self.learning_rate_multipliers = [self.W_learning_rate_multiplier,
                                          self.b_learning_rate_multiplier]


class AtrousConvolution2D(Convolution2D):
    '''Atrous Convolution operator for filtering windows of two-dimensional inputs.
    A.k.a dilated convolution or convolution with holes.
    When using this layer as the first layer in a model,
    provide the keyword argument `input_shape`
    (tuple of integers, does not include the sample axis),
    e.g. `input_shape=(3, 128, 128)` for 128x128 RGB pictures.

    # Examples

    ```python
        # apply a 3x3 convolution with atrous rate 2x2 and 64 output filters on a 256x256 image:
        model = Sequential()
        model.add(AtrousConvolution2D(64, 3, 3, atrous_rate=(2,2), border_mode='valid', input_shape=(3, 256, 256)))
        # now the actual kernel size is dilated from 3x3 to 5x5 (3+(3-1)*(2-1)=5)
        # thus model.output_shape == (None, 64, 252, 252)
    ```

    # Arguments
        nb_filter: Number of convolution filters to use.
        nb_row: Number of rows in the convolution kernel.
        nb_col: Number of columns in the convolution kernel.
        init: name of initialization function for the weights of the layer
            (see [initializations](../initializations.md)), or alternatively,
            Theano function to use for weights initialization.
            This parameter is only relevant if you don't pass
            a `weights` argument.
        activation: name of activation function to use
            (see [activations](../activations.md)),
            or alternatively, elementwise Theano function.
            If you don't specify anything, no activation is applied
            (ie. "linear" activation: a(x) = x).
        weights: list of numpy arrays to set as initial weights.
        border_mode: 'valid', 'same' or 'full'. ('full' requires the Theano backend.)
        subsample: tuple of length 2. Factor by which to subsample output.
            Also called strides elsewhere.
        atrous_rate: tuple of length 2. Factor for kernel dilation.
            Also called filter_dilation elsewhere.
        W_regularizer: instance of [WeightRegularizer](../regularizers.md)
            (eg. L1 or L2 regularization), applied to the main weights matrix.
        b_regularizer: instance of [WeightRegularizer](../regularizers.md),
            applied to the bias.
        activity_regularizer: instance of [ActivityRegularizer](../regularizers.md),
            applied to the network output.
        W_constraint: instance of the [constraints](../constraints.md) module
            (eg. maxnorm, nonneg), applied to the main weights matrix.
        b_constraint: instance of the [constraints](../constraints.md) module,
            applied to the bias.
        dim_ordering: 'th' or 'tf'. In 'th' mode, the channels dimension
            (the depth) is at index 1, in 'tf' mode is it at index 3.
            It defaults to the `image_dim_ordering` value found in your
            Keras config file at `~/.keras/keras.json`.
            If you never set it, then it will be "tf".
        bias: whether to include a bias (i.e. make the layer affine rather than linear).

    # Input shape
        4D tensor with shape:
        `(samples, channels, rows, cols)` if dim_ordering='th'
        or 4D tensor with shape:
        `(samples, rows, cols, channels)` if dim_ordering='tf'.

    # Output shape
        4D tensor with shape:
        `(samples, nb_filter, new_rows, new_cols)` if dim_ordering='th'
        or 4D tensor with shape:
        `(samples, new_rows, new_cols, nb_filter)` if dim_ordering='tf'.
        `rows` and `cols` values might have changed due to padding.

    # References
        - [Multi-Scale Context Aggregation by Dilated Convolutions](https://arxiv.org/abs/1511.07122)
    '''
    def __init__(self, nb_filter, nb_row, nb_col,
                 init='glorot_uniform', activation=None, weights=None,
                 border_mode='valid', subsample=(1, 1),
                 atrous_rate=(1, 1), dim_ordering='default',
                 W_regularizer=None, b_regularizer=None, activity_regularizer=None,
                 W_constraint=None, b_constraint=None,
                 bias=True, **kwargs):
        if dim_ordering == 'default':
            dim_ordering = K.image_dim_ordering()

        if border_mode not in {'valid', 'same', 'full'}:
            raise ValueError('Invalid border mode for AtrousConv2D:', border_mode)

        self.atrous_rate = tuple(atrous_rate)

        super(AtrousConvolution2D, self).__init__(nb_filter, nb_row, nb_col,
                                                  init=init,
                                                  activation=activation,
                                                  weights=weights,
                                                  border_mode=border_mode,
                                                  subsample=subsample,
                                                  dim_ordering=dim_ordering,
                                                  W_regularizer=W_regularizer,
                                                  b_regularizer=b_regularizer,
                                                  activity_regularizer=activity_regularizer,
                                                  W_constraint=W_constraint,
                                                  b_constraint=b_constraint,
                                                  bias=bias,
                                                  **kwargs)

    def get_output_shape_for(self, input_shape):
        if self.dim_ordering == 'th':
            rows = input_shape[2]
            cols = input_shape[3]
        elif self.dim_ordering == 'tf':
            rows = input_shape[1]
            cols = input_shape[2]
        else:
            raise ValueError('Invalid dim_ordering:', self.dim_ordering)

        rows = conv_output_length(rows, self.nb_row, self.border_mode,
                                  self.subsample[0],
                                  dilation=self.atrous_rate[0])
        cols = conv_output_length(cols, self.nb_col, self.border_mode,
                                  self.subsample[1],
                                  dilation=self.atrous_rate[1])

        if self.dim_ordering == 'th':
            return (input_shape[0], self.nb_filter, rows, cols)
        elif self.dim_ordering == 'tf':
            return (input_shape[0], rows, cols, self.nb_filter)

    def call(self, x, mask=None):
        output = K.conv2d(x, self.W, strides=self.subsample,
                          border_mode=self.border_mode,
                          dim_ordering=self.dim_ordering,
                          filter_shape=self.W_shape,
                          filter_dilation=self.atrous_rate)
        if self.bias:
            if self.dim_ordering == 'th':
                output += K.reshape(self.b, (1, self.nb_filter, 1, 1))
            elif self.dim_ordering == 'tf':
                output += K.reshape(self.b, (1, 1, 1, self.nb_filter))
            else:
                raise ValueError('Invalid dim_ordering:', self.dim_ordering)
        output = self.activation(output)
        return output

    def get_config(self):
        config = {'atrous_rate': self.atrous_rate}
        base_config = super(AtrousConvolution2D, self).get_config()
        return dict(list(base_config.items()) + list(config.items()))


class SeparableConvolution2D(Layer):
    '''Separable convolution operator for 2D inputs.

    Separable convolutions consist in first performing
    a depthwise spatial convolution
    (which acts on each input channel separately)
    followed by a pointwise convolution which mixes together the resulting
    output channels. The `depth_multiplier` argument controls how many
    output channels are generated per input channel in the depthwise step.

    Intuitively, separable convolutions can be understood as
    a way to factorize a convolution kernel into two smaller kernels,
    or as an extreme version of an Inception block.

    When using this layer as the first layer in a model,
    provide the keyword argument `input_shape`
    (tuple of integers, does not include the sample axis),
    e.g. `input_shape=(3, 128, 128)` for 128x128 RGB pictures.

    # Theano warning

    This layer is only available with the
    TensorFlow backend for the time being.

    # Arguments
        nb_filter: Number of convolution filters to use.
        nb_row: Number of rows in the convolution kernel.
        nb_col: Number of columns in the convolution kernel.
        init: name of initialization function for the weights of the layer
            (see [initializations](../initializations.md)), or alternatively,
            Theano function to use for weights initialization.
            This parameter is only relevant if you don't pass
            a `weights` argument.
        activation: name of activation function to use
            (see [activations](../activations.md)),
            or alternatively, elementwise Theano function.
            If you don't specify anything, no activation is applied
            (ie. "linear" activation: a(x) = x).
        weights: list of numpy arrays to set as initial weights.
        border_mode: 'valid' or 'same'.
        subsample: tuple of length 2. Factor by which to subsample output.
            Also called strides elsewhere.
        depth_multiplier: how many output channel to use per input channel
            for the depthwise convolution step.
        depthwise_regularizer: instance of [WeightRegularizer](../regularizers.md)
            (eg. L1 or L2 regularization), applied to the depthwise weights matrix.
        pointwise_regularizer: instance of [WeightRegularizer](../regularizers.md)
            (eg. L1 or L2 regularization), applied to the pointwise weights matrix.
        b_regularizer: instance of [WeightRegularizer](../regularizers.md),
            applied to the bias.
        activity_regularizer: instance of [ActivityRegularizer](../regularizers.md),
            applied to the network output.
        depthwise_constraint: instance of the [constraints](../constraints.md) module
            (eg. maxnorm, nonneg), applied to the depthwise weights matrix.
        pointwise_constraint: instance of the [constraints](../constraints.md) module
            (eg. maxnorm, nonneg), applied to the pointwise weights matrix.
        b_constraint: instance of the [constraints](../constraints.md) module,
            applied to the bias.
        dim_ordering: 'th' or 'tf'. In 'th' mode, the channels dimension
            (the depth) is at index 1, in 'tf' mode is it at index 3.
            It defaults to the `image_dim_ordering` value found in your
            Keras config file at `~/.keras/keras.json`.
            If you never set it, then it will be "tf".
        bias: whether to include a bias
            (i.e. make the layer affine rather than linear).

    # Input shape
        4D tensor with shape:
        `(samples, channels, rows, cols)` if dim_ordering='th'
        or 4D tensor with shape:
        `(samples, rows, cols, channels)` if dim_ordering='tf'.

    # Output shape
        4D tensor with shape:
        `(samples, nb_filter, new_rows, new_cols)` if dim_ordering='th'
        or 4D tensor with shape:
        `(samples, new_rows, new_cols, nb_filter)` if dim_ordering='tf'.
        `rows` and `cols` values might have changed due to padding.
    '''
    def __init__(self, nb_filter, nb_row, nb_col,
                 init='glorot_uniform', activation=None, weights=None,
                 border_mode='valid', subsample=(1, 1),
                 depth_multiplier=1, dim_ordering='default',
                 depthwise_regularizer=None, pointwise_regularizer=None,
                 b_regularizer=None, activity_regularizer=None,
                 depthwise_constraint=None, pointwise_constraint=None,
                 b_constraint=None,
                 bias=True, **kwargs):

        if K.backend() != 'tensorflow':
            raise RuntimeError('SeparableConv2D is only available '
                               'with TensorFlow for the time being.')

        if dim_ordering == 'default':
            dim_ordering = K.image_dim_ordering()

        if border_mode not in {'valid', 'same'}:
            raise ValueError('Invalid border mode for SeparableConv2D:', border_mode)

        if border_mode not in {'valid', 'same'}:
            raise ValueError('Invalid border mode for SeparableConv2D:', border_mode)
        self.nb_filter = nb_filter
        self.nb_row = nb_row
        self.nb_col = nb_col
        self.init = initializations.get(init, dim_ordering=dim_ordering)
        self.activation = activations.get(activation)
        if border_mode not in {'valid', 'same'}:
            raise ValueError('border_mode must be in {valid, same}.')
        self.border_mode = border_mode
        self.subsample = tuple(subsample)
        self.depth_multiplier = depth_multiplier
        if dim_ordering not in {'tf', 'th'}:
            raise ValueError('dim_ordering must be in {tf, th}.')
        self.dim_ordering = dim_ordering

        self.depthwise_regularizer = regularizers.get(depthwise_regularizer)
        self.pointwise_regularizer = regularizers.get(pointwise_regularizer)
        self.b_regularizer = regularizers.get(b_regularizer)
        self.activity_regularizer = regularizers.get(activity_regularizer)

        self.depthwise_constraint = constraints.get(depthwise_constraint)
        self.pointwise_constraint = constraints.get(pointwise_constraint)
        self.b_constraint = constraints.get(b_constraint)

        self.bias = bias
        self.input_spec = [InputSpec(ndim=4)]
        self.initial_weights = weights
        super(SeparableConvolution2D, self).__init__(**kwargs)

    def build(self, input_shape):
        if self.dim_ordering == 'th':
            stack_size = input_shape[1]
            depthwise_shape = (self.depth_multiplier, stack_size, self.nb_row, self.nb_col)
            pointwise_shape = (self.nb_filter, self.depth_multiplier * stack_size, 1, 1)
        elif self.dim_ordering == 'tf':
            stack_size = input_shape[3]
            depthwise_shape = (self.nb_row, self.nb_col, stack_size, self.depth_multiplier)
            pointwise_shape = (1, 1, self.depth_multiplier * stack_size, self.nb_filter)
        else:
            raise ValueError('Invalid dim_ordering:', self.dim_ordering)

        self.depthwise_kernel = self.add_weight(depthwise_shape,
                                                initializer=self.init,
                                                regularizer=self.depthwise_regularizer,
                                                constraint=self.depthwise_constraint,
                                                name='{}_depthwise_kernel'.format(self.name))
        self.pointwise_kernel = self.add_weight(pointwise_shape,
                                                initializer=self.init,
                                                regularizer=self.pointwise_regularizer,
                                                constraint=self.pointwise_constraint,
                                                name='{}_pointwise_kernel'.format(self.name))
        if self.bias:
            self.b = self.add_weight((self.nb_filter,),
                                     initializer='zero',
                                     name='{}_b'.format(self.name),
                                     regularizer=self.b_regularizer,
                                     constraint=self.b_constraint)
        else:
            self.b = None

        if self.initial_weights is not None:
            self.set_weights(self.initial_weights)
            del self.initial_weights
        self.built = True

    def get_output_shape_for(self, input_shape):
        if self.dim_ordering == 'th':
            rows = input_shape[2]
            cols = input_shape[3]
        elif self.dim_ordering == 'tf':
            rows = input_shape[1]
            cols = input_shape[2]
        else:
            raise ValueError('Invalid dim_ordering:', self.dim_ordering)

        rows = conv_output_length(rows, self.nb_row,
                                  self.border_mode, self.subsample[0])
        cols = conv_output_length(cols, self.nb_col,
                                  self.border_mode, self.subsample[1])

        if self.dim_ordering == 'th':
            return (input_shape[0], self.nb_filter, rows, cols)
        elif self.dim_ordering == 'tf':
            return (input_shape[0], rows, cols, self.nb_filter)
        else:
            raise ValueError('Invalid dim_ordering:', self.dim_ordering)

    def call(self, x, mask=None):
        output = K.separable_conv2d(x, self.depthwise_kernel,
                                    self.pointwise_kernel,
                                    strides=self.subsample,
                                    border_mode=self.border_mode,
                                    dim_ordering=self.dim_ordering)
        if self.bias:
            if self.dim_ordering == 'th':
                output += K.reshape(self.b, (1, self.nb_filter, 1, 1))
            elif self.dim_ordering == 'tf':
                output += K.reshape(self.b, (1, 1, 1, self.nb_filter))
            else:
                raise ValueError('Invalid dim_ordering:', self.dim_ordering)
        output = self.activation(output)
        return output

    def get_config(self):
        config = {'nb_filter': self.nb_filter,
                  'nb_row': self.nb_row,
                  'nb_col': self.nb_col,
                  'init': self.init.__name__,
                  'activation': self.activation.__name__,
                  'border_mode': self.border_mode,
                  'subsample': self.subsample,
                  'depth_multiplier': self.depth_multiplier,
                  'dim_ordering': self.dim_ordering,
                  'depthwise_regularizer': self.depthwise_regularizer.get_config() if self.depthwise_regularizer else None,
                  'pointwise_regularizer': self.depthwise_regularizer.get_config() if self.depthwise_regularizer else None,
                  'b_regularizer': self.b_regularizer.get_config() if self.b_regularizer else None,
                  'activity_regularizer': self.activity_regularizer.get_config() if self.activity_regularizer else None,
                  'depthwise_constraint': self.depthwise_constraint.get_config() if self.depthwise_constraint else None,
                  'pointwise_constraint': self.pointwise_constraint.get_config() if self.pointwise_constraint else None,
                  'b_constraint': self.b_constraint.get_config() if self.b_constraint else None,
                  'bias': self.bias}
        base_config = super(SeparableConvolution2D, self).get_config()
        return dict(list(base_config.items()) + list(config.items()))


class Convolution3D(Layer):
    '''Convolution operator for filtering windows of three-dimensional inputs.
    When using this layer as the first layer in a model,
    provide the keyword argument `input_shape`
    (tuple of integers, does not include the sample axis),
    e.g. `input_shape=(3, 10, 128, 128)` for 10 frames of 128x128 RGB pictures.

    # Arguments
        nb_filter: Number of convolution filters to use.
        kernel_dim1: Length of the first dimension in the convolution kernel.
        kernel_dim2: Length of the second dimension in the convolution kernel.
        kernel_dim3: Length of the third dimension in the convolution kernel.
        init: name of initialization function for the weights of the layer
            (see [initializations](../initializations.md)), or alternatively,
            Theano function to use for weights initialization.
            This parameter is only relevant if you don't pass
            a `weights` argument.
        activation: name of activation function to use
            (see [activations](../activations.md)),
            or alternatively, elementwise Theano function.
            If you don't specify anything, no activation is applied
            (ie. "linear" activation: a(x) = x).
        weights: list of Numpy arrays to set as initial weights.
        border_mode: 'valid', 'same' or 'full'. ('full' requires the Theano backend.)
        subsample: tuple of length 3. Factor by which to subsample output.
            Also called strides elsewhere.
            Note: 'subsample' is implemented by slicing the output of conv3d with strides=(1,1,1).
        W_regularizer: instance of [WeightRegularizer](../regularizers.md)
            (eg. L1 or L2 regularization), applied to the main weights matrix.
        b_regularizer: instance of [WeightRegularizer](../regularizers.md),
            applied to the bias.
        activity_regularizer: instance of [ActivityRegularizer](../regularizers.md),
            applied to the network output.
        W_constraint: instance of the [constraints](../constraints.md) module
            (eg. maxnorm, nonneg), applied to the main weights matrix.
        b_constraint: instance of the [constraints](../constraints.md) module,
            applied to the bias.
        dim_ordering: 'th' or 'tf'. In 'th' mode, the channels dimension
            (the depth) is at index 1, in 'tf' mode is it at index 4.
            It defaults to the `image_dim_ordering` value found in your
            Keras config file at `~/.keras/keras.json`.
            If you never set it, then it will be "tf".
        bias: whether to include a bias (i.e. make the layer affine rather than linear).

    # Input shape
        5D tensor with shape:
        `(samples, channels, conv_dim1, conv_dim2, conv_dim3)` if dim_ordering='th'
        or 5D tensor with shape:
        `(samples, conv_dim1, conv_dim2, conv_dim3, channels)` if dim_ordering='tf'.

    # Output shape
        5D tensor with shape:
        `(samples, nb_filter, new_conv_dim1, new_conv_dim2, new_conv_dim3)` if dim_ordering='th'
        or 5D tensor with shape:
        `(samples, new_conv_dim1, new_conv_dim2, new_conv_dim3, nb_filter)` if dim_ordering='tf'.
        `new_conv_dim1`, `new_conv_dim2` and `new_conv_dim3` values might have changed due to padding.
    '''

    def __init__(self, nb_filter, kernel_dim1, kernel_dim2, kernel_dim3,
                 init='glorot_uniform', activation=None, weights=None,
                 border_mode='valid', subsample=(1, 1, 1), dim_ordering='default',
                 W_regularizer=None, b_regularizer=None, activity_regularizer=None,
                 W_constraint=None, b_constraint=None,
                 bias=True, **kwargs):
        if dim_ordering == 'default':
            dim_ordering = K.image_dim_ordering()

        if border_mode not in {'valid', 'same', 'full'}:
            raise ValueError('Invalid border mode for Convolution3D:', border_mode)
        self.nb_filter = nb_filter
        self.kernel_dim1 = kernel_dim1
        self.kernel_dim2 = kernel_dim2
        self.kernel_dim3 = kernel_dim3
        self.init = initializations.get(init, dim_ordering=dim_ordering)
        self.activation = activations.get(activation)
        self.border_mode = border_mode
        self.subsample = tuple(subsample)
        if dim_ordering not in {'tf', 'th'}:
            raise ValueError('dim_ordering must be in {tf, th}.')
        self.dim_ordering = dim_ordering

        self.W_regularizer = regularizers.get(W_regularizer)
        self.b_regularizer = regularizers.get(b_regularizer)
        self.activity_regularizer = regularizers.get(activity_regularizer)

        self.W_constraint = constraints.get(W_constraint)
        self.b_constraint = constraints.get(b_constraint)

        self.bias = bias
        self.input_spec = [InputSpec(ndim=5)]
        self.initial_weights = weights
        super(Convolution3D, self).__init__(**kwargs)

    def build(self, input_shape):
        assert len(input_shape) == 5
        self.input_spec = [InputSpec(shape=input_shape)]

        if self.dim_ordering == 'th':
            stack_size = input_shape[1]
            self.W_shape = (self.nb_filter, stack_size,
                            self.kernel_dim1, self.kernel_dim2, self.kernel_dim3)
        elif self.dim_ordering == 'tf':
            stack_size = input_shape[4]
            self.W_shape = (self.kernel_dim1, self.kernel_dim2, self.kernel_dim3,
                            stack_size, self.nb_filter)
        else:
            raise ValueError('Invalid dim_ordering:', self.dim_ordering)

        self.W = self.add_weight(self.W_shape,
                                 initializer=self.init,
                                 name='{}_W'.format(self.name),
                                 regularizer=self.W_regularizer,
                                 constraint=self.W_constraint)
        if self.bias:
            self.b = self.add_weight((self.nb_filter,),
                                     initializer='zero',
                                     name='{}_b'.format(self.name),
                                     regularizer=self.b_regularizer,
                                     constraint=self.b_constraint)
        else:
            self.b = None

        if self.initial_weights is not None:
            self.set_weights(self.initial_weights)
            del self.initial_weights
        self.built = True

    def get_output_shape_for(self, input_shape):
        if self.dim_ordering == 'th':
            conv_dim1 = input_shape[2]
            conv_dim2 = input_shape[3]
            conv_dim3 = input_shape[4]
        elif self.dim_ordering == 'tf':
            conv_dim1 = input_shape[1]
            conv_dim2 = input_shape[2]
            conv_dim3 = input_shape[3]
        else:
            raise ValueError('Invalid dim_ordering:', self.dim_ordering)

        conv_dim1 = conv_output_length(conv_dim1, self.kernel_dim1,
                                       self.border_mode, self.subsample[0])
        conv_dim2 = conv_output_length(conv_dim2, self.kernel_dim2,
                                       self.border_mode, self.subsample[1])
        conv_dim3 = conv_output_length(conv_dim3, self.kernel_dim3,
                                       self.border_mode, self.subsample[2])

        if self.dim_ordering == 'th':
            return (input_shape[0], self.nb_filter, conv_dim1, conv_dim2, conv_dim3)
        elif self.dim_ordering == 'tf':
            return (input_shape[0], conv_dim1, conv_dim2, conv_dim3, self.nb_filter)
        else:
            raise ValueError('Invalid dim_ordering:', self.dim_ordering)

    def call(self, x, mask=None):
        input_shape = self.input_spec[0].shape
        output = K.conv3d(x, self.W, strides=self.subsample,
                          border_mode=self.border_mode,
                          dim_ordering=self.dim_ordering,
                          volume_shape=input_shape,
                          filter_shape=self.W_shape)
        if self.bias:
            if self.dim_ordering == 'th':
                output += K.reshape(self.b, (1, self.nb_filter, 1, 1, 1))
            elif self.dim_ordering == 'tf':
                output += K.reshape(self.b, (1, 1, 1, 1, self.nb_filter))
            else:
                raise ValueError('Invalid dim_ordering:', self.dim_ordering)
        output = self.activation(output)
        return output

    def get_config(self):
        config = {'nb_filter': self.nb_filter,
                  'kernel_dim1': self.kernel_dim1,
                  'kernel_dim2': self.kernel_dim2,
                  'kernel_dim3': self.kernel_dim3,
                  'dim_ordering': self.dim_ordering,
                  'init': self.init.__name__,
                  'activation': self.activation.__name__,
                  'border_mode': self.border_mode,
                  'subsample': self.subsample,
                  'W_regularizer': self.W_regularizer.get_config() if self.W_regularizer else None,
                  'b_regularizer': self.b_regularizer.get_config() if self.b_regularizer else None,
                  'activity_regularizer': self.activity_regularizer.get_config() if self.activity_regularizer else None,
                  'W_constraint': self.W_constraint.get_config() if self.W_constraint else None,
                  'b_constraint': self.b_constraint.get_config() if self.b_constraint else None,
                  'bias': self.bias}
        base_config = super(Convolution3D, self).get_config()
        return dict(list(base_config.items()) + list(config.items()))


class UpSampling1D(Layer):
    '''Repeat each temporal step `length` times along the time axis.

    # Arguments
        length: integer. Upsampling factor.

    # Input shape
        3D tensor with shape: `(samples, steps, features)`.

    # Output shape
        3D tensor with shape: `(samples, upsampled_steps, features)`.
    '''

    def __init__(self, length=2, **kwargs):
        self.length = length
        self.input_spec = [InputSpec(ndim=3)]
        super(UpSampling1D, self).__init__(**kwargs)

    def get_output_shape_for(self, input_shape):
        length = self.length * input_shape[1] if input_shape[1] is not None else None
        return (input_shape[0], length, input_shape[2])

    def call(self, x, mask=None):
        output = K.repeat_elements(x, self.length, axis=1)
        return output

    def get_config(self):
        config = {'length': self.length}
        base_config = super(UpSampling1D, self).get_config()
        return dict(list(base_config.items()) + list(config.items()))


class UpSampling2D(Layer):
    '''Repeat the rows and columns of the data
    by size[0] and size[1] respectively.

    # Arguments
        size: tuple of 2 integers. The upsampling factors for rows and columns.
        dim_ordering: 'th' or 'tf'.
            In 'th' mode, the channels dimension (the depth)
            is at index 1, in 'tf' mode is it at index 3.
            It defaults to the `image_dim_ordering` value found in your
            Keras config file at `~/.keras/keras.json`.
            If you never set it, then it will be "tf".

    # Input shape
        4D tensor with shape:
        `(samples, channels, rows, cols)` if dim_ordering='th'
        or 4D tensor with shape:
        `(samples, rows, cols, channels)` if dim_ordering='tf'.

    # Output shape
        4D tensor with shape:
        `(samples, channels, upsampled_rows, upsampled_cols)` if dim_ordering='th'
        or 4D tensor with shape:
        `(samples, upsampled_rows, upsampled_cols, channels)` if dim_ordering='tf'.
    '''

    def __init__(self, size=(2, 2), dim_ordering='default', **kwargs):
        if dim_ordering == 'default':
            dim_ordering = K.image_dim_ordering()
        self.size = tuple(size)
        if dim_ordering not in {'tf', 'th'}:
            raise ValueError('dim_ordering must be in {tf, th}.')
        self.dim_ordering = dim_ordering
        self.input_spec = [InputSpec(ndim=4)]
        super(UpSampling2D, self).__init__(**kwargs)

    def get_output_shape_for(self, input_shape):
        if self.dim_ordering == 'th':
            width = self.size[0] * input_shape[2] if input_shape[2] is not None else None
            height = self.size[1] * input_shape[3] if input_shape[3] is not None else None
            return (input_shape[0],
                    input_shape[1],
                    width,
                    height)
        elif self.dim_ordering == 'tf':
            width = self.size[0] * input_shape[1] if input_shape[1] is not None else None
            height = self.size[1] * input_shape[2] if input_shape[2] is not None else None
            return (input_shape[0],
                    width,
                    height,
                    input_shape[3])
        else:
            raise ValueError('Invalid dim_ordering:', self.dim_ordering)

    def call(self, x, mask=None):
        return K.resize_images(x, self.size[0], self.size[1],
                               self.dim_ordering)

    def get_config(self):
        config = {'size': self.size}
        base_config = super(UpSampling2D, self).get_config()
        return dict(list(base_config.items()) + list(config.items()))


class UpSampling3D(Layer):
    '''Repeat the first, second and third dimension of the data
    by size[0], size[1] and size[2] respectively.

    # Arguments
        size: tuple of 3 integers. The upsampling factors for dim1, dim2 and dim3.
        dim_ordering: 'th' or 'tf'.
            In 'th' mode, the channels dimension (the depth)
            is at index 1, in 'tf' mode is it at index 4.
            It defaults to the `image_dim_ordering` value found in your
            Keras config file at `~/.keras/keras.json`.
            If you never set it, then it will be "tf".

    # Input shape
        5D tensor with shape:
        `(samples, channels, dim1, dim2, dim3)` if dim_ordering='th'
        or 5D tensor with shape:
        `(samples, dim1, dim2, dim3, channels)` if dim_ordering='tf'.

    # Output shape
        5D tensor with shape:
        `(samples, channels, upsampled_dim1, upsampled_dim2, upsampled_dim3)` if dim_ordering='th'
        or 5D tensor with shape:
        `(samples, upsampled_dim1, upsampled_dim2, upsampled_dim3, channels)` if dim_ordering='tf'.
    '''

    def __init__(self, size=(2, 2, 2), dim_ordering='default', **kwargs):
        if dim_ordering == 'default':
            dim_ordering = K.image_dim_ordering()
        self.size = tuple(size)
        if dim_ordering not in {'tf', 'th'}:
            raise ValueError('dim_ordering must be in {tf, th}.')
        self.dim_ordering = dim_ordering
        self.input_spec = [InputSpec(ndim=5)]
        super(UpSampling3D, self).__init__(**kwargs)

    def get_output_shape_for(self, input_shape):
        if self.dim_ordering == 'th':
            dim1 = self.size[0] * input_shape[2] if input_shape[2] is not None else None
            dim2 = self.size[1] * input_shape[3] if input_shape[3] is not None else None
            dim3 = self.size[2] * input_shape[4] if input_shape[4] is not None else None
            return (input_shape[0],
                    input_shape[1],
                    dim1,
                    dim2,
                    dim3)
        elif self.dim_ordering == 'tf':
            dim1 = self.size[0] * input_shape[1] if input_shape[1] is not None else None
            dim2 = self.size[1] * input_shape[2] if input_shape[2] is not None else None
            dim3 = self.size[2] * input_shape[3] if input_shape[3] is not None else None
            return (input_shape[0],
                    dim1,
                    dim2,
                    dim3,
                    input_shape[4])
        else:
            raise ValueError('Invalid dim_ordering:', self.dim_ordering)

    def call(self, x, mask=None):
        return K.resize_volumes(x, self.size[0], self.size[1], self.size[2],
                                self.dim_ordering)

    def get_config(self):
        config = {'size': self.size}
        base_config = super(UpSampling3D, self).get_config()
        return dict(list(base_config.items()) + list(config.items()))


class ZeroPadding1D(Layer):
    '''Zero-padding layer for 1D input (e.g. temporal sequence).

    # Arguments
        padding: int, or tuple of int (length 2), or dictionary.
            - If int:
            How many zeros to add at the beginning and end of
            the padding dimension (axis 1).
            - If tuple of int (length 2)
            How many zeros to add at the beginning and at the end of
            the padding dimension, in order '(left_pad, right_pad)'.
            - If dictionary: should contain the keys
            {'left_pad', 'right_pad'}.
            If any key is missing, default value of 0 will be used for the missing key.

    # Input shape
        3D tensor with shape (samples, axis_to_pad, features)

    # Output shape
        3D tensor with shape (samples, padded_axis, features)
    '''

    def __init__(self, padding=1, **kwargs):
        super(ZeroPadding1D, self).__init__(**kwargs)
        self.padding = padding

        if isinstance(padding, int):
            self.left_pad = padding
            self.right_pad = padding

        elif isinstance(padding, dict):
            if set(padding.keys()) <= {'left_pad', 'right_pad'}:
                self.left_pad = padding.get('left_pad', 0)
                self.right_pad = padding.get('right_pad', 0)
            else:
                raise ValueError('Unexpected key found in `padding` dictionary. '
                                 'Keys have to be in {"left_pad", "right_pad"}. '
                                 'Found: ' + str(padding.keys()))
        else:
            padding = tuple(padding)
            if len(padding) != 2:
                raise ValueError('`padding` should be int, or dict with keys '
                                 '{"left_pad", "right_pad"}, or tuple of length 2. '
                                 'Found: ' + str(padding))
            self.left_pad = padding[0]
            self.right_pad = padding[1]
        self.input_spec = [InputSpec(ndim=3)]

    def get_output_shape_for(self, input_shape):
        length = input_shape[1] + self.left_pad + self.right_pad if input_shape[1] is not None else None
        return (input_shape[0],
                length,
                input_shape[2])

    def call(self, x, mask=None):
        return K.asymmetric_temporal_padding(x, left_pad=self.left_pad, right_pad=self.right_pad)

    def get_config(self):
        config = {'padding': self.padding}
        base_config = super(ZeroPadding1D, self).get_config()
        return dict(list(base_config.items()) + list(config.items()))


class ZeroPadding2D(Layer):
    '''Zero-padding layer for 2D input (e.g. picture).

    # Arguments
        padding: tuple of int (length 2), or tuple of int (length 4), or dictionary.
            - If tuple of int (length 2):
            How many zeros to add at the beginning and end of
            the 2 padding dimensions (rows and cols).
            - If tuple of int (length 4):
            How many zeros to add at the beginning and at the end of
            the 2 padding dimensions (rows and cols), in the order
            '(top_pad, bottom_pad, left_pad, right_pad)'.
            - If dictionary: should contain the keys
            {'top_pad', 'bottom_pad', 'left_pad', 'right_pad'}.
            If any key is missing, default value of 0 will be used for the missing key.
        dim_ordering: 'th' or 'tf'.
            In 'th' mode, the channels dimension (the depth)
            is at index 1, in 'tf' mode is it at index 3.
            It defaults to the `image_dim_ordering` value found in your
            Keras config file at `~/.keras/keras.json`.
            If you never set it, then it will be "tf".

    # Input shape
        4D tensor with shape:
        `(samples, channels, rows, cols)` if dim_ordering='th'
        or 4D tensor with shape:
        `(samples, rows, cols, channels)` if dim_ordering='tf'.

    # Output shape
        4D tensor with shape:
        `(samples, channels, padded_rows, padded_cols)` if dim_ordering='th'
        or 4D tensor with shape:
        `(samples, padded_rows, padded_cols, channels)` if dim_ordering='tf'.
    '''

    def __init__(self,
                 padding=(1, 1),
                 dim_ordering='default',
                 **kwargs):
        super(ZeroPadding2D, self).__init__(**kwargs)
        if dim_ordering == 'default':
            dim_ordering = K.image_dim_ordering()

        self.padding = padding
        if isinstance(padding, dict):
            if set(padding.keys()) <= {'top_pad', 'bottom_pad', 'left_pad', 'right_pad'}:
                self.top_pad = padding.get('top_pad', 0)
                self.bottom_pad = padding.get('bottom_pad', 0)
                self.left_pad = padding.get('left_pad', 0)
                self.right_pad = padding.get('right_pad', 0)
            else:
                raise ValueError('Unexpected key found in `padding` dictionary. '
                                 'Keys have to be in {"top_pad", "bottom_pad", '
                                 '"left_pad", "right_pad"}.'
                                 'Found: ' + str(padding.keys()))
        else:
            padding = tuple(padding)
            if len(padding) == 2:
                self.top_pad = padding[0]
                self.bottom_pad = padding[0]
                self.left_pad = padding[1]
                self.right_pad = padding[1]
            elif len(padding) == 4:
                self.top_pad = padding[0]
                self.bottom_pad = padding[1]
                self.left_pad = padding[2]
                self.right_pad = padding[3]
            else:
                raise TypeError('`padding` should be tuple of int '
                                'of length 2 or 4, or dict. '
                                'Found: ' + str(padding))

        if dim_ordering not in {'tf', 'th'}:
            raise ValueError('dim_ordering must be in {tf, th}.')
        self.dim_ordering = dim_ordering
        self.input_spec = [InputSpec(ndim=4)]

    def get_output_shape_for(self, input_shape):
        if self.dim_ordering == 'th':
            rows = input_shape[2] + self.top_pad + self.bottom_pad if input_shape[2] is not None else None
            cols = input_shape[3] + self.left_pad + self.right_pad if input_shape[3] is not None else None
            return (input_shape[0],
                    input_shape[1],
                    rows,
                    cols)
        elif self.dim_ordering == 'tf':
            rows = input_shape[1] + self.top_pad + self.bottom_pad if input_shape[1] is not None else None
            cols = input_shape[2] + self.left_pad + self.right_pad if input_shape[2] is not None else None
            return (input_shape[0],
                    rows,
                    cols,
                    input_shape[3])
        else:
            raise ValueError('Invalid dim_ordering:', self.dim_ordering)

    def call(self, x, mask=None):
        return K.asymmetric_spatial_2d_padding(x,
                                               top_pad=self.top_pad,
                                               bottom_pad=self.bottom_pad,
                                               left_pad=self.left_pad,
                                               right_pad=self.right_pad,
                                               dim_ordering=self.dim_ordering)

    def get_config(self):
        config = {'padding': self.padding}
        base_config = super(ZeroPadding2D, self).get_config()
        return dict(list(base_config.items()) + list(config.items()))


class ZeroPadding3D(Layer):
    '''Zero-padding layer for 3D data (spatial or spatio-temporal).

    # Arguments
        padding: tuple of int (length 3)
            How many zeros to add at the beginning and end of
            the 3 padding dimensions (axis 3, 4 and 5).
            Currently only symmetric padding is supported.
        dim_ordering: 'th' or 'tf'.
            In 'th' mode, the channels dimension (the depth)
            is at index 1, in 'tf' mode is it at index 4.
            It defaults to the `image_dim_ordering` value found in your
            Keras config file at `~/.keras/keras.json`.
            If you never set it, then it will be "tf".

    # Input shape
        5D tensor with shape:
        (samples, depth, first_axis_to_pad, second_axis_to_pad, third_axis_to_pad)

    # Output shape
        5D tensor with shape:
        (samples, depth, first_padded_axis, second_padded_axis, third_axis_to_pad)
    '''

    def __init__(self, padding=(1, 1, 1), dim_ordering='default', **kwargs):
        super(ZeroPadding3D, self).__init__(**kwargs)
        if dim_ordering == 'default':
            dim_ordering = K.image_dim_ordering()
        self.padding = tuple(padding)
        if dim_ordering not in {'tf', 'th'}:
            raise ValueError('dim_ordering must be in {tf, th}.')
        self.dim_ordering = dim_ordering
        self.input_spec = [InputSpec(ndim=5)]

    def get_output_shape_for(self, input_shape):
        if self.dim_ordering == 'th':
            dim1 = input_shape[2] + 2 * self.padding[0] if input_shape[2] is not None else None
            dim2 = input_shape[3] + 2 * self.padding[1] if input_shape[3] is not None else None
            dim3 = input_shape[4] + 2 * self.padding[2] if input_shape[4] is not None else None
            return (input_shape[0],
                    input_shape[1],
                    dim1,
                    dim2,
                    dim3)
        elif self.dim_ordering == 'tf':
            dim1 = input_shape[1] + 2 * self.padding[0] if input_shape[1] is not None else None
            dim2 = input_shape[2] + 2 * self.padding[1] if input_shape[2] is not None else None
            dim3 = input_shape[3] + 2 * self.padding[2] if input_shape[3] is not None else None
            return (input_shape[0],
                    dim1,
                    dim2,
                    dim3,
                    input_shape[4])
        else:
            raise ValueError('Invalid dim_ordering:', self.dim_ordering)

    def call(self, x, mask=None):
        return K.spatial_3d_padding(x, padding=self.padding,
                                    dim_ordering=self.dim_ordering)

    def get_config(self):
        config = {'padding': self.padding}
        base_config = super(ZeroPadding3D, self).get_config()
        return dict(list(base_config.items()) + list(config.items()))


class CompactBilinearPooling(Layer):
    '''Compact Bilinear Pooling
    # Arguments:
        d: dimension of the compact bilinear feature

    # References:
        - [Multimodal Compact Bilinear Pooling for Visual Question Answering and Visual Grounding](http://arxiv.org/pdf/1606.01847v2.pdf)
    '''

    def __init__(self, d, return_extra=False, conv_type='conv', **kwargs):
        self.h = [None, None]
        self.s = [None, None]
        self.return_extra = return_extra
        self.conv_type = conv_type
        self.d = d
        self.shape_in = None

        # layer parameters
        self.inbound_nodes = []
        self.outbound_nodes = []
        self.constraints = {}
        self.regularizers = []
        self.trainable_weights = []
        self.non_trainable_weights = []
        self.supports_masking = True
        self.trainable = False
        self.uses_learning_phase = False
        self.input_spec = None  # compatible with whatever
        super(CompactBilinearPooling, self).__init__(**kwargs)

    def build(self, input_shapes):
        self.trainable_weights = []
        self.nmodes = len(input_shapes)
        assert self.nmodes == 2
        self.shape_in = input_shapes
        for i in range(self.nmodes):
            if self.h[i] is None:
                self.h[i] = np.random.random_integers(0, self.d-1, size=(input_shapes[i][1],))
                self.h[i] = K.variable(self.h[i], dtype='int64', name='h'+str(i))
            if self.s[i] is None:
                self.s[i] = (np.floor(np.random.uniform(0, 2, size=(input_shapes[i][1],)))*2-1).astype('int64')
                self.s[i] = K.variable(self.s[i], dtype='int64', name='s'+str(i))
        self.non_trainable_weights = [self.h[i] for i in range(self.nmodes)] + [self.s[i] for i in range(self.nmodes)]

        self.built = True

    def compute_mask(self, input, input_mask=None):
        to_return = []
        if input_mask is None or not any([m is not None for m in input_mask]):
            to_return.append(None)
        else:
            to_return = input_mask[0]
        if self.return_extra:
            for i in range(self.nmodes):
                to_return += [None, None, None, None]
        return to_return # +[None]

    def multimodal_compact_bilinear(self, x):
        v = [[]] * self.nmodes

        if self.conv_type == 'conv':
            for i in range(self.nmodes):
                v[i] = K.count_sketch(self.h[i], self.s[i], x[i], self.d)
            out = K.conv1d(v[0], v[1])

        elif self.conv_type == 'fft':
            raise NotImplementedError()
            fft_v = [[]] * self.nmodes
            acum_fft = 1.0
            for i in range(self.nmodes):
                '''
                v[i] = K.count_sketch(self.h[i], self.s[i], x[i], self.d)
                fft_v[i] = K.fft(v[i])
                acum_fft *= fft_v[i]
                '''
                v[i] = K.count_sketch(self.h[i], self.s[i], x[i], self.d)
                zeros_pad = K.zeros_like(v[i])[:, :-1]
                v_in = K.concatenate([zeros_pad,
                                      v[i],
                                      zeros_pad], axis=1)
                fft_v[i] = K.fft(v_in)
                prev = K.cast(K.floor(self.d/2.), 'int16')
                post = K.cast(K.ceil(self.d/2.), 'int16')
                acum_fft *= K.concatenate((fft_v[i][:, -post:], fft_v[i][:, :prev]), axis=1)

            out = K.cast(K.ifft(acum_fft), dtype='float32')

        else:
            raise NotImplementedError()

        if self.return_extra:
            # TODO: remove fft_v and acum_fft from all returns
            raise NotImplementedError("return_extra not implemented")
            return [out]+v+fft_v+[acum_fft]
        else:
            return out

    def call(self, x, mask=None):
        if type(x) is not list or len(x) < 2:
            raise Exception('CompactBilinearPooling must be called on a list of tensors '
                            '(at least 2). Got: ' + str(x))
        if len(self.shape_in[0]) > 2:
            x = [x[i].dimshuffle(tuple([0] + range(2, len(self.shape_in[0])) + [1])) for i in range(self.nmodes)]
            x = [K.reshape(x[i], tuple([-1] + [self.shape_in[0][1]])) for i in range(self.nmodes)]
            ##x = [K.reshape(K.dimshuffle(x[i], tuple([0]+range(2,len(self.shape_in))+[1])), tuple([-1] + [self.shape_in[1]])) for i in range(self.nmodes)]
        y = self.multimodal_compact_bilinear(x)
        if len(self.shape_in[0]) > 2:
            y = K.reshape(y, tuple([-1] + self.shape_in[0][2:] + [self.d]))
            y.dimshuffle(tuple([0, -1] + range(1, len(self.shape_in[0]) - 1)))
            ##y = K.dimshuffle(K.reshape(y, tuple([-1] + self.shape_in[0][2:] + [self.d])), tuple([0,-1]+range(1,len(self.shape_in)-1)))
        if self.return_extra:
            return y+self.h+self.s
        return y

    def get_config(self):
        config = {'d': self.d,
                  'return_extra': self.return_extra,
                  'conv_type': self.conv_type}
        base_config = super(CompactBilinearPooling, self).get_config()
        return dict(list(base_config.items()) + list(config.items()))

    def get_output_shape_for(self, input_shape):
        assert type(input_shape) is list  # must have mutiple input shape tuples
        shapes = []
        shapes.append(tuple([input_shape[0][0], self.d] + list(input_shape[0][2:])))
        if self.return_extra:
            for s in input_shape: # v
                shapes.append(tuple([np.prod(s[0]+list(s[2:])), self.d]))
            for s in input_shape: # fft_v
                shapes.append(tuple([np.prod(s[0]+list(s[2:])), self.d]))
            shapes.append(tuple([np.prod(s[0]+list(s[2:])), self.d])) # acum_fft
            for s in input_shape: # h
                shapes.append(tuple([s[1],1]))
            for s in input_shape: # s
                shapes.append(tuple([s[1],1]))
            return shapes
        else:
            return shapes[0]


    
class BilinearPooling(Layer):
    '''Compact Bilinear Pooling

    # References:
        - [Multimodal Compact Bilinear Pooling for Visual Question Answering and Visual Grounding](http://arxiv.org/pdf/1606.01847v2.pdf)
    '''

    def __init__(self, d, **kwargs):

        # layer parameters
        self.inbound_nodes = []
        self.outbound_nodes = []
        self.constraints = {}
        self.regularizers = []
        self.trainable_weights = []
        self.non_trainable_weights = []
        self.supports_masking = True
        self.trainable = False
        self.uses_learning_phase = False
        self.input_spec = None  # compatible with whatever
        super(BilinearPooling, self).__init__(**kwargs)

    def build(self, input_shapes):
        self.trainable_weights = []
        self.nmodes = len(input_shapes)
        for i,s in enumerate(input_shapes):
            if s != input_shapes[0]:
                raise Exception('The input size of all vectors must be the same: '
                                'shape of vector on position '+str(i)+' (0-based) '+str(s)+' != shape of vector on position 0 '+str(input_shapes[0]))
        self.built = True

    def compute_mask(self, input, input_mask=None):
        if input_mask is None or not any([m is not None for m in input_mask]):
            return None
        else:
            return input_mask[0]

    def multimodal_bilinear(self, x):
        v = [[]] * self.nmodes
        acum_fft = 1.0
        for i in range(self.nmodes):
            acum_fft = acum_fft * K.fft(x[i])
        return K.cast(K.ifft(acum_fft), dtype='float32')

    def call(self, x, mask=None):
        if type(x) is not list or len(x) <= 1:
            raise Exception('BilinearPooling must be called on a list of tensors '
                            '(at least 2). Got: ' + str(x))
        return self.multimodal_bilinear(x)
    
    def get_config(self):
        base_config = super(BilinearPooling, self).get_config()
        return dict(list(base_config.items()))

    def get_output_shape_for(self, input_shape):
        assert type(input_shape) is list  # must have mutiple input shape tuples
        return input_shape[0]


class CountSketch(Layer):
    '''Count Sketch vector compacting
    # Arguments:
        d: dimension of the output compact representation
    '''

    def __init__(self, d, return_extra=False, **kwargs):
        self.h = [None, None]
        self.s = [None, None]
        self.return_extra = return_extra
        self.d = d

        # layer parameters
        self.inbound_nodes = []
        self.outbound_nodes = []
        self.constraints = {}
        self.regularizers = []
        self.trainable_weights = []
        self.non_trainable_weights = []
        self.supports_masking = True
        self.trainable = False
        self.uses_learning_phase = False
        self.input_spec = None  # compatible with whatever
        self.built = False
        super(CountSketch, self).__init__(**kwargs)

    def build(self, input_shapes):
        if not self.built:
            self.trainable_weights = []
            self.nmodes = len(input_shapes)
            for i in range(self.nmodes):
                if self.h[i] is None:
                    self.h[i] = np.random.random_integers(0, self.d-1, size=(input_shapes[i][1],))
                    self.h[i] = K.variable(self.h[i], dtype='int64', name='h'+str(i))
                if self.s[i] is None:
                    self.s[i] =  (np.floor(np.random.uniform(0, 2, size=(input_shapes[i][1],)))*2-1).astype('int64')
                    self.s[i] = K.variable(self.s[i], dtype='int64', name='s'+str(i))
        self.built = True

    def compute_mask(self, input, input_mask=None):
        to_return = []
        if input_mask is None or not any([m is not None for m in input_mask]):
            for i in range(len(input_mask)):
                to_return.append(None)
        else:
            to_return =  input_mask
        if self.return_extra:
            for i in range(self.nmodes):
                to_return += [None, None]
        return to_return

    def compact(self, x):
        v = [[]] * self.nmodes
        for i in range(self.nmodes):
            v[i] = K.count_sketch(self.h[i], self.s[i], x[i], self.d)
        return v

    def call(self, x, mask=None):
        if type(x) is not list or len(x) <= 1:
            raise Exception('CountSketch must be called on a list of tensors.')
        y = self.compact(x)
        if self.return_extra:
            return y+self.h+self.s
        return y   
 
    def get_config(self):
        config = {'d': self.d,
                  'return_extra': self.return_extra}
        base_config = super(CountSketch, self).get_config()
        return dict(list(base_config.items()) + list(config.items()))

    def get_output_shape_for(self, input_shape):
        assert type(input_shape) is list  # must have mutiple input shape tuples
        shapes = []
        for s in input_shape:
            shapes.append(tuple([s[0], self.d]))
        if self.return_extra:
            for i in range(self.nmodes):
                shapes.append(tuple([input_shape[i][1],1]))
            for i in range(self.nmodes):
                shapes.append(tuple([input_shape[i][1],1]))
        return shapes


class Cropping1D(Layer):
    '''Cropping layer for 1D input (e.g. temporal sequence).
    It crops along the time dimension (axis 1).

    # Arguments
        cropping: tuple of int (length 2)
            How many units should be trimmed off at the beginning and end of
            the cropping dimension (axis 1).

    # Input shape
        3D tensor with shape (samples, axis_to_crop, features)

    # Output shape
        3D tensor with shape (samples, cropped_axis, features)
    '''

    def __init__(self, cropping=(1, 1), **kwargs):
        super(Cropping1D, self).__init__(**kwargs)
        self.cropping = tuple(cropping)
        if len(self.cropping) != 2:
            raise ValueError('`cropping` must be a tuple length of 2.')
        self.input_spec = [InputSpec(ndim=3)]

    def build(self, input_shape):
        self.input_spec = [InputSpec(shape=input_shape)]
        self.built = True

    def get_output_shape_for(self, input_shape):
        if input_shape[1] is not None:
            length = input_shape[1] - self.cropping[0] - self.cropping[1]
        else:
            length = None
        return (input_shape[0],
                length,
                input_shape[2])

    def call(self, x, mask=None):
        return x[:, self.cropping[0]:-self.cropping[1], :]

    def get_config(self):
        config = {'cropping': self.cropping}
        base_config = super(Cropping1D, self).get_config()
        return dict(list(base_config.items()) + list(config.items()))


class Cropping2D(Layer):
    '''Cropping layer for 2D input (e.g. picture).
    It crops along spatial dimensions, i.e. width and height.

    # Arguments
        cropping: tuple of tuple of int (length 2)
            How many units should be trimmed off at the beginning and end of
            the 2 cropping dimensions (width, height).
        dim_ordering: 'th' or 'tf'.
            In 'th' mode, the channels dimension (the depth)
            is at index 1, in 'tf' mode is it at index 3.
            It defaults to the `image_dim_ordering` value found in your
            Keras config file at `~/.keras/keras.json`.
            If you never set it, then it will be "tf".

    # Input shape
        4D tensor with shape:
        (samples, depth, first_axis_to_crop, second_axis_to_crop)

    # Output shape
        4D tensor with shape:
        (samples, depth, first_cropped_axis, second_cropped_axis)

    # Examples

    ```python
        # Crop the input 2D images or feature maps
        model = Sequential()
        model.add(Cropping2D(cropping=((2, 2), (4, 4)), input_shape=(3, 28, 28)))
        # now model.output_shape == (None, 3, 24, 20)
        model.add(Convolution2D(64, 3, 3, border_mode='same))
        model.add(Cropping2D(cropping=((2, 2), (2, 2))))
        # now model.output_shape == (None, 64, 20, 16)

    ```

    '''

    def __init__(self, cropping=((0, 0), (0, 0)), dim_ordering='default', **kwargs):
        super(Cropping2D, self).__init__(**kwargs)
        if dim_ordering == 'default':
            dim_ordering = K.image_dim_ordering()
        self.cropping = tuple(cropping)
        if len(self.cropping) != 2:
            raise ValueError('`cropping` must be a tuple length of 2.')
        if len(self.cropping[0]) != 2:
            raise ValueError('`cropping[0]` must be a tuple length of 2.')
        if len(self.cropping[1]) != 2:
            raise ValueError('`cropping[1]` must be a tuple length of 2.')
        if dim_ordering not in {'tf', 'th'}:
            raise ValueError('dim_ordering must be in {tf, th}.')
        self.dim_ordering = dim_ordering
        self.input_spec = [InputSpec(ndim=4)]

    def build(self, input_shape):
        self.input_spec = [InputSpec(shape=input_shape)]
        self.built = True

    def get_output_shape_for(self, input_shape):
        if self.dim_ordering == 'th':
            return (input_shape[0],
                    input_shape[1],
                    input_shape[2] - self.cropping[0][0] - self.cropping[0][1],
                    input_shape[3] - self.cropping[1][0] - self.cropping[1][1])
        elif self.dim_ordering == 'tf':
            return (input_shape[0],
                    input_shape[1] - self.cropping[0][0] - self.cropping[0][1],
                    input_shape[2] - self.cropping[1][0] - self.cropping[1][1],
                    input_shape[3])
        else:
            raise ValueError('Invalid dim_ordering:', self.dim_ordering)

    def call(self, x, mask=None):
        if self.dim_ordering == 'th':
            return x[:,
                     :,
                     self.cropping[0][0]:-self.cropping[0][1],
                     self.cropping[1][0]:-self.cropping[1][1]]
        elif self.dim_ordering == 'tf':
            return x[:,
                     self.cropping[0][0]:-self.cropping[0][1],
                     self.cropping[1][0]:-self.cropping[1][1],
                     :]

    def get_config(self):
        config = {'cropping': self.cropping}
        base_config = super(Cropping2D, self).get_config()
        return dict(list(base_config.items()) + list(config.items()))


class Cropping3D(Layer):
    '''Cropping layer for 3D data (e.g. spatial or spatio-temporal).

    # Arguments
        cropping: tuple of tuple of int (length 3)
            How many units should be trimmed off at the beginning and end of
            the 3 cropping dimensions (kernel_dim1, kernel_dim2, kernerl_dim3).
        dim_ordering: 'th' or 'tf'.
            In 'th' mode, the channels dimension (the depth)
            is at index 1, in 'tf' mode is it at index 4.
            It defaults to the `image_dim_ordering` value found in your
            Keras config file at `~/.keras/keras.json`.
            If you never set it, then it will be "tf".

    # Input shape
        5D tensor with shape:
        (samples, depth, first_axis_to_crop, second_axis_to_crop, third_axis_to_crop)

    # Output shape
        5D tensor with shape:
        (samples, depth, first_cropped_axis, second_cropped_axis, third_cropped_axis)

    '''

    def __init__(self, cropping=((1, 1), (1, 1), (1, 1)),
                 dim_ordering='default', **kwargs):
        super(Cropping3D, self).__init__(**kwargs)
        if dim_ordering == 'default':
            dim_ordering = K.image_dim_ordering()
        self.cropping = tuple(cropping)
        if len(self.cropping) != 3:
            raise ValueError('`cropping` must be a tuple length of 3.')
        if len(self.cropping[0]) != 2:
            raise ValueError('`cropping[0]` must be a tuple length of 2.')
        if len(self.cropping[1]) != 2:
            raise ValueError('`cropping[1]` must be a tuple length of 2.')
        if len(self.cropping[2]) != 2:
            raise ValueError('`cropping[2]` must be a tuple length of 2.')
        if dim_ordering not in {'tf', 'th'}:
            raise ValueError('dim_ordering must be in {tf, th}.')
        self.dim_ordering = dim_ordering
        self.input_spec = [InputSpec(ndim=5)]

    def build(self, input_shape):
        self.input_spec = [InputSpec(shape=input_shape)]
        self.built = True

    def get_output_shape_for(self, input_shape):
        if self.dim_ordering == 'th':
            dim1 = input_shape[2] - self.cropping[0][0] - self.cropping[0][1] if input_shape[2] is not None else None
            dim2 = input_shape[3] - self.cropping[1][0] - self.cropping[1][1] if input_shape[3] is not None else None
            dim3 = input_shape[4] - self.cropping[2][0] - self.cropping[2][1] if input_shape[4] is not None else None
            return (input_shape[0],
                    input_shape[1],
                    dim1,
                    dim2,
                    dim3)
        elif self.dim_ordering == 'tf':
            dim1 = input_shape[1] - self.cropping[0][0] - self.cropping[0][1] if input_shape[1] is not None else None
            dim2 = input_shape[2] - self.cropping[1][0] - self.cropping[1][1] if input_shape[2] is not None else None
            dim3 = input_shape[3] - self.cropping[2][0] - self.cropping[2][1] if input_shape[3] is not None else None
            return (input_shape[0],
                    dim1,
                    dim2,
                    dim3,
                    input_shape[4])
        else:
            raise ValueError('Invalid dim_ordering:', self.dim_ordering)

    def call(self, x, mask=None):
        if self.dim_ordering == 'th':
            return x[:,
                     :,
                     self.cropping[0][0]:-self.cropping[0][1],
                     self.cropping[1][0]:-self.cropping[1][1],
                     self.cropping[2][0]:-self.cropping[2][1]]
        elif self.dim_ordering == 'tf':
            return x[:,
                     self.cropping[0][0]:-self.cropping[0][1],
                     self.cropping[1][0]:-self.cropping[1][1],
                     self.cropping[2][0]:-self.cropping[2][1],
                     :]

    def get_config(self):
        config = {'cropping': self.cropping}
        base_config = super(Cropping3D, self).get_config()
        return dict(list(base_config.items()) + list(config.items()))


# Aliases

Conv1D = Convolution1D
Conv2D = Convolution2D
Conv3D = Convolution3D
Deconv2D = Deconvolution2D
AtrousConv1D = AtrousConvolution1D
AtrousConv2D = AtrousConvolution2D
SeparableConv2D = SeparableConvolution2D

