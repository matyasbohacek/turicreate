# -*- coding: utf-8 -*-
# Copyright © 2018 Apple Inc. All rights reserved.
#
# Use of this source code is governed by a BSD-3-clause license that can
# be found in the LICENSE.txt file or at https://opensource.org/licenses/BSD-3-Clause
"""
Python API for MPS neural network backend
"""
from __future__ import print_function as _
from __future__ import division as _
from __future__ import absolute_import as _
import os as _os
import ctypes as _ctypes
import numpy as _np
import six as _six
from copy import deepcopy as _deepcopy
from turicreate import config as _tc_config
from ._internal_utils import _mac_ver


class MpsGraphNetworkType(object):
    kSingleReLUGraphNet = 0
    kSingleConvGraphNet = 1
    kSingleBNGraphNet   = 2
    kSingleMPGraphNet   = 3
    kODGraphNet         = 4


class MpsGraphMode(object):
    Train           = 0
    TrainReturnGrad = 1
    Inference       = 2


class MpsLowLevelNetworkType(object):
    kSingleReLUNet      = 0
    kSingleConvNet      = 1
    kSingleBNNet        = 2
    kSingleMPNet        = 3
    kSingle1DConvNet    = 4
    kODNet              = 5
    kSingleDropOut      = 6
    kSingleFcNet        = 7
    kSingleSoftMaxNet   = 8
    kActivityClassifierNet= 9
    kSingleLstmNet      = 10

class MpsLowLevelMode(object):
    kLowLevelModeTrain      = 0
    kLowLevelModeInference  = 1
    kLowLevelModeTest       = 2


def _decode_bytes_to_native_string(s):
    if _six.PY3:
        return s.decode()
    else:
        return s


def mps_to_mxnet(weight):
    if weight.ndim == 1:
        return weight
    elif weight.ndim == 4:
        return weight.transpose(0, 3, 1, 2)
    else:
        raise ValueError('Not supported')


def mxnet_to_mps(weight):
    if weight.ndim == 1:
        return weight
    elif weight.ndim == 4:
        return weight.transpose(0, 2, 3, 1)
    else:
        raise ValueError('Not supported')

def ac_weights_mps_to_mxnet(mps_weights, lstm_h_size):
    import mxnet as _mx
    aux_params = {}
    mxnet_weights = {}
    for key in mps_weights:
        if key == 'conv_weight':
            mxnet_weights[key] = _mx.nd.array(_np.squeeze(mps_to_mxnet(mps_weights[key])))
        elif "running" in key:
            w = _mx.nd.array(_np.squeeze(mps_weights[key]))
            new_key = key.replace("running", "moving")
            aux_params[new_key] = w
        else:
            w = _mx.nd.array(_np.squeeze(mps_weights[key]))
            mxnet_weights[key] = w

    bias_shape = mxnet_weights['lstm_h2h_i_bias'].shape

    mock_lstm = _mx.rnn.LSTMCell(prefix='lstm_', num_hidden=lstm_h_size)
    for gate_name in mock_lstm._gate_names:
        mxnet_weights['lstm_i2h' + gate_name + '_bias'] = _mx.nd.array(_np.zeros((bias_shape)))

    mxnet_weights = mock_lstm.pack_weights(mxnet_weights)

    return mxnet_weights, aux_params

def ac_weights_mxnet_to_mps(arg_params, aux_params, lstm_h_size):
    import mxnet as _mx
    mxnet_weights = arg_params.copy()
    mxnet_weights.update(aux_params)

    mock_lstm = _mx.rnn.LSTMCell(prefix='lstm_', num_hidden=lstm_h_size)
    mxnet_weights = mock_lstm.unpack_weights(mxnet_weights)
    mps_weights = {}
    for key in mxnet_weights:
        w = mxnet_weights[key].asnumpy()
        if 'moving' in key:
            new_key = key.replace("moving", "running")
            mps_weights[new_key] = w
        elif key.startswith('conv') and key.endswith('weight'):
            w = mxnet_weights[key].asnumpy()
            mps_weights[key] = mxnet_to_mps(w[..., _np.newaxis, :])
        elif key.startswith('dense') and key.endswith('weight'):
            mps_weights[key] = w[:, _np.newaxis, _np.newaxis]
        else:
            mps_weights[key] = w

    return mps_weights

def mxnet_network_to_mps_params(net_params):
    mps_net_params = {}
    for k in net_params:
        mps_net_params[k] = mxnet_to_mps(net_params[k].data().asnumpy())
    return mps_net_params


def _prepare_network_parameters(arg_dict):
    items = []
    for name, arr in arg_dict.items():
        if isinstance(arr, _np.ndarray):
            if not arr.flags.c_contiguous:
                arr = arr.copy()
        else:
            arr = _np.array(arr, dtype=_np.float32)
        assert arr.flags.c_contiguous, "Input weights must be row-major"
        items.append((name, arr.astype(_np.float32)))

    name = (_ctypes.c_char_p * len(items))()
    arr = (_ctypes.c_void_p * len(items))()
    sz = (_ctypes.c_int64 * len(items))()
    for i in range(len(items)):
        name[i] = _ctypes.c_char_p(items[i][0].encode())
        sz[i] = _ctypes.c_int64(items[i][1].size)
        arr[i] = _ctypes.c_void_p(items[i][1].ctypes.data)
    return items, name, arr, sz


_g_TCMPS_LIB = None

def _load_tcmps_lib():
    """
    Load global singleton of tcmps lib handler.

    This function is used not used at the top level, so
    that the shared library is loaded lazily only when needed.
    """
    global _g_TCMPS_LIB
    if _g_TCMPS_LIB is None:
        # This library requires macOS 10.14 or above
        if _mac_ver() < (10, 14):
            return None

        file_dir = _os.path.dirname(__file__)
        lib_path = _os.path.abspath(_os.path.join(file_dir, 'libtcmps.dylib'))
        try:
            _g_TCMPS_LIB = _ctypes.CDLL(lib_path, _ctypes.RTLD_LOCAL)
        except OSError:
            pass
    return _g_TCMPS_LIB


def has_fast_mps_support():
    """
    Returns True if the environment has MPS backend support
    and a high-power (fast) device is available.
    """
    lib = _load_tcmps_lib()
    if lib is None:
        return False

    c_bool = _ctypes.c_bool()
    ret = lib.TCMPSHasHighPowerMetalDevice(_ctypes.byref(c_bool))
    return ret == 0 and c_bool.value


def use_mps():
    """
    Returns True if MPS can and should be used.
    """
    return _tc_config.get_num_gpus() != 0 and has_fast_mps_support()


def mps_device_name():
    """
    Returns name of MPS device that will be used, else None.
    """
    lib = _load_tcmps_lib()
    if lib is None:
        return None

    n = 256
    c_name = (_ctypes.c_char * n)()
    ret = lib.TCMPSMetalDeviceName(_ctypes.byref(c_name), _ctypes.c_int32(n))
    if ret == 0:
        return _decode_bytes_to_native_string(c_name.value)
    else:
        return None


def mps_device_memory_limit():
    """
    Returns the memory size in bytes that can be effectively allocated on the
    MPS device that will be used, or None if no suitable device is available.
    """
    lib = _load_tcmps_lib()
    if lib is None:
        return None

    c_size = _ctypes.c_uint64()
    ret = lib.TCMPSMetalDeviceMemoryLimit(_ctypes.byref(c_size))
    return c_size.value if ret == 0 else None


def _xavier_init(weight):
    shape = weight.shape
    dim = len(shape)
    if dim < 2:
        raise ValueError("Xavier init expects at least 2 dimensions")

    scale = 1
    n_in = shape[0]
    n_out = shape[-1]

    if dim > 2:
        scale = _np.prod(shape[1:-1])

    c = _np.sqrt(3. / (0.5 * (n_in * scale + n_out * scale)))
    return _np.random.uniform(-c, c, shape).astype(_np.float32)


class MpsFloatArray(object):
    """
    A Python wrapper owning a C++ float_array created by the TCMPS backend.

    This class exists to simplify conversions from numpy to the TCMPS format and
    to simplify memory management. Instances usually just serve as arguments to
    the methods on MpsGraphAPI and MpsLowLevelAPI, below.
    """

    def __init__(self, x):
        """Wrap a numpy array"""

        assert isinstance(x, _np.ndarray)

        # Load TCMPS backend library.
        self._LIB = _load_tcmps_lib()
        assert self._LIB is not None, "Cannot use MpsFloatArray without libtcmps.dylib"

        # Convert the input if necessary to contain a contiguous float array.
        self.data = x
        if self.data.dtype != _np.float32:
            self.data = self.data.astype(_np.float32)
        if not self.data.flags.c_contiguous:
            self.data = self.data.copy()
        assert self.data.flags.c_contiguous, "Data must be row-major"

        # Obtain a pointer to the internal float array (and obtain size).
        data_ptr = self.data.ctypes.data_as(_ctypes.POINTER(_ctypes.c_void_p))
        sz = _ctypes.c_size_t(self.data.size)

        # Copy the shape so that it contains a size_t array.
        self.shape = _np.array(self.data.shape).astype(_np.uintp)
        shape_ptr = self.shape.ctypes.data_as(_ctypes.POINTER(_ctypes.c_size_t))
        dim = _ctypes.c_size_t(self.data.ndim)

        # Call into TCMPS to create a wrapper around self.data and self.shape.
        # Those two properties must outlive the resulting self.handle.
        self.handle = _ctypes.c_void_p()
        status_code = self._LIB.TCMPSCreateFloatArray(
            _ctypes.byref(self.handle), data_ptr, sz, shape_ptr, dim)
        assert status_code == 0, "Error calling TCMPSCreateFloatArray"

    def __del__(self):
        status_code = self._LIB.TCMPSDeleteFloatArray(self.handle)
        assert status_code == 0, "Error calling TCMPSDeleteFloatArray"

class MpsFloatArrayIterator(object):
    """
    A Python wrapper owning a sequence of name/float_array pairs output from the
    TCMPS backend.

    This class exists to simplify conversions from the output of TCMPS export
    functions. It implements the iterator protocol, so that a Python dict
    (mapping parameter names for numpy arrays) can be initialized directly from
    an instance of this class.
    """

    def __init__(self, handle):
        """Wrap the output of a TCMPSExport* function."""
        self._LIB = _load_tcmps_lib()
        assert self._LIB is not None, "Cannot use MpsFloatArrayIterator without libtcmps.dylib"

        self.handle = handle

    def __del__(self):
        status_code = self._LIB.TCMPSDeleteFloatArrayMapIterator(self.handle)
        assert status_code == 0, "Error calling TCMPSDeleteFloatArrayMapIterator"

    def __iter__(self):
        return self

    def __next__(self):
        # Create C variables that will serve as out parameters for TCMPS.
        name_ptr = _ctypes.c_char_p()                    # char* name_ptr
        data_ptr = _ctypes.POINTER(_ctypes.c_float)()    # float* data_ptr
        shape_ptr = _ctypes.POINTER(_ctypes.c_size_t)()  # size_t* shape_ptr
        dim = _ctypes.c_size_t()                         # size_t dim

        # Obtain pointers into memory owned by the C++ object self.handle.
        status_code = self._LIB.TCMPSNextFloatArray(
            self.handle, _ctypes.byref(name_ptr), _ctypes.byref(data_ptr),
            _ctypes.byref(shape_ptr), _ctypes.byref(dim))

        if status_code != 0:
            raise StopIteration

        # Wrap size_t* as size_t[dim]
        shape_buf = (_ctypes.c_size_t * dim.value).from_address(
            _ctypes.addressof(shape_ptr.contents))

        # Convert size_t[dim] to numpy
        shape = _np.fromiter(shape_buf, _np.uint64, dim.value)

        # Wrap float* to float[size]
        size = _np.prod(shape)
        data_buf = (_ctypes.c_float * size).from_address(
            _ctypes.addressof(data_ptr.contents))

        # Convert float[size] to numpy
        array = _np.fromiter(data_buf, _np.float32, size).reshape(shape)

        # Convert char* to Python string
        name = _decode_bytes_to_native_string(name_ptr.value)

        return (name, array)

    def next(self):
        return self.__next__()


#----------------------------------------------------------
#
#  MPS Graph level API, currently used by Object detector
#
#----------------------------------------------------------


class MpsGraphAPI(object):
    def __init__(self, network_id):
        self.handle = _ctypes.c_void_p()
        self._LIB = _load_tcmps_lib()
        assert self._LIB is not None, "Cannot use MpsGraphAPI without libtcmps.dylib"
        self._LIB.TCMPSCreateGraphModule(_ctypes.byref(self.handle))
        self._buf_out_fp16 = None
        self._buf_loss = None
        self._ishape = None
        self._oshape = None
        self.network_id = network_id
        # current state, for reloading weights
        self._cur_config = {}
        self._cur_learning_rate = None

    def __del__(self):
        self._LIB.TCMPSDeleteGraphModule(self.handle)

    def init(self, n, c_in, h_in, w_in, c_out, h_out, w_out, config=None, weights=None):
        if weights is None:
            weights = {}
        if config is None:
            config = {
                'learning_rate': 1e-3,
                'gradient_clipping': 0.025,
                'weight_decay': 0.00005,
                'momentum': 0.9,
            }

        self._mode = int(config.get('mode', MpsGraphMode.TrainReturnGrad))
        self._is_train = self._mode in {MpsGraphMode.TrainReturnGrad, MpsGraphMode.Train}

        config_items, config_name, config_arr, config_sz = _prepare_network_parameters(config)
        weights_items, weights_name, weights_arr, weights_sz = _prepare_network_parameters(weights)
        self._LIB.TCMPSInitGraph(
            self.handle,
            self.network_id,
            _ctypes.c_int32(n),
            _ctypes.c_int32(c_in),
            _ctypes.c_int32(h_in),
            _ctypes.c_int32(w_in),
            _ctypes.c_int32(c_out),
            _ctypes.c_int32(h_out),
            _ctypes.c_int32(w_out),
            config_name, config_arr, config_sz, _ctypes.c_int32(len(config_items)),
            weights_name, weights_arr, weights_sz, _ctypes.c_int32(len(weights_items)),
        )
        self._cur_config = _deepcopy(config)
        if self._mode == MpsGraphMode.TrainReturnGrad:
            sz = n * c_in * h_in * w_in
        else:
            sz = n * c_out * h_out * w_out
        self._buf_out_fp16 = (_ctypes.c_float * (sz // 2))()
        self._buf_loss = (_ctypes.c_float * n)()
        self._ishape = (n, h_in, w_in, c_in)
        self._oshape = (n, h_out, w_out, c_out)

    # Submits an input batch to the model. The model will process the input
    # asynchronously. This call must be matched with a corresponding call to
    # wait_for_batch. Label data is required for models initialized with
    # MpsGraphMode.Train; grad data is required for models initialized with
    # MpsGraphMode.TrainReturnGrad.
    def start_batch(self, input, label=None, grad=None):
        assert input.shape == self._ishape

        input_array = MpsFloatArray(input)

        if self._mode == MpsGraphMode.Train:
            assert label is not None, "Training graph requires labels"
            assert label.shape == self._oshape

            label_array = MpsFloatArray(label)
            self._LIB.TCMPSStartTrainingBatchGraph(
                self.handle, input_array.handle, label_array.handle)

        elif self._mode == MpsGraphMode.TrainReturnGrad:
            assert grad is not None, "Training graph (without loss) requires gradient"
            assert grad.shape == self._oshape

            grad_array = MpsFloatArray(grad)
            self._LIB.TCMPSStartTrainReturnGradBatchGraph(
                self.handle, input_array.handle, grad_array.handle)

        else:
            self._LIB.TCMPSStartInferenceBatchGraph(
                self.handle, input_array.handle)

    # Waits for a previously submitted batch to complete and returns the output.
    # For models initialized with MpsGraphMode.Train, the return value is the
    # loss. For models initialized with MpsGraphMode.Inference, the return value
    # is the model predictions. For models initialized with
    # MpsGraphMode.TrainReturnGrad, the return value is the gradient for the
    # input layer.
    def wait_for_batch(self):
        if self._mode == MpsGraphMode.Train:
            self._LIB.TCMPSWaitForTrainingBatchGraph(self.handle, _ctypes.byref(self._buf_loss))
            loss = _np.frombuffer(self._buf_loss, dtype=_np.float32).copy()
            return loss
        elif self._mode == MpsGraphMode.TrainReturnGrad:
            self._LIB.TCMPSWaitForTrainReturnGradBatchGraph(self.handle, _ctypes.byref(self._buf_out_fp16))
            raw_out = _np.frombuffer(self._buf_out_fp16, dtype=_np.float16)
            out = raw_out.reshape(self._ishape).astype(_np.float32).copy()
            return out
        else:
            self._LIB.TCMPSWaitForInferenceBatchGraph(self.handle, _ctypes.byref(self._buf_out_fp16))
            raw_out = _np.frombuffer(self._buf_out_fp16, dtype=_np.float16)
            out = raw_out.reshape(self._oshape).astype(_np.float32).copy()
            return out

    def set_learning_rate(self, new_lr):
        self._cur_learning_rate = new_lr
        self._LIB.TCMPSSetLearningRateGraph(self.handle, _ctypes.c_float(new_lr))

    def load(self, weights):
        self._LIB.TCMPSDeleteGraphModule(self.handle)
        self.handle = _ctypes.c_void_p()
        self._LIB.TCMPSCreateGraphModule(_ctypes.byref(self.handle),
                                 _ctypes.c_int(self._mode))
        n, h_in, w_in, c_in = self._ishape
        _, h_out, w_out, c_out = self._oshape
        self.init(n, c_in, h_in, w_in, c_out, h_out, w_out,
                  config=self._cur_config, weights=weights)
        # Reload state
        if self._cur_learning_rate:
            self.set_learning_rate(self._cur_learning_rate)

    def _num_params(self):
        num = _ctypes.c_int32(0)
        self._LIB.TCMPSNumParamsGraph(self.handle, _ctypes.byref(num))
        return num.value

    def export(self):
        iter_handle = _ctypes.c_void_p()
        status_code = self._LIB.TCMPSExportGraph(self.handle,
                                                 _ctypes.byref(iter_handle))
        assert status_code == 0
        return dict(MpsFloatArrayIterator(iter_handle))


#----------------------------------------------------------
#
#  MPS Graph level API, currently used by Activity Classifier
#
#----------------------------------------------------------

class MpsLowLevelAPI(object):
    def __init__(self, network_id=MpsLowLevelNetworkType.kActivityClassifierNet):
        self.handle = _ctypes.c_void_p()
        self._LIB = _load_tcmps_lib()
        assert self._LIB is not None, "Cannot use MpsLowLevelAPI without libtcmps.dylib"
        self._LIB.TCMPSCreateCNNModule(_ctypes.byref(self.handle))
        self._buf = None
        self._buf_g = None
        self._ishape = None
        self._oshape = None
        self.network_id = network_id

    def __del__(self):
        self._LIB.TCMPSDeleteCNNModule(self.handle)

    def init(self, n, c_in, h_in, w_in, c_out, h_out, w_out, updater=1, config={}):
        config_items, config_name, config_arr, config_sz = _prepare_network_parameters(config)
        self._LIB.TCMPSInit(
            self.handle,
            self.network_id,
            _ctypes.c_int32(n),
            _ctypes.c_int32(c_in),
            _ctypes.c_int32(h_in),
            _ctypes.c_int32(w_in),
            _ctypes.c_int32(c_out),
            _ctypes.c_int32(h_out),
            _ctypes.c_int32(w_out),
            _ctypes.c_int32(updater),
            config_name, config_arr, config_sz, _ctypes.c_int32(len(config_items)),
        )
        sz = n * c_out * h_out * w_out
        self._buf = (_ctypes.c_float * sz)()
        sz = n * c_in * h_in * w_in
        self._buf_g = (_ctypes.c_float * sz)()

        if (h_in == 1 and h_out == 1):
            self._ishape = (n, w_in, c_in)
            self._oshape = (n, w_out, c_out)
        else:
            self._ishape = (n, h_in, w_in, c_in)
            self._oshape = (n, h_out, w_out, c_out)

    def forward(self, x, is_train=True):
        assert x.shape == self._ishape
        x_array = MpsFloatArray(x)

        self._LIB.TCMPSForward(
            self.handle, x_array.handle, _ctypes.byref(self._buf),
            _ctypes.c_bool(is_train))

        output = (_np.frombuffer(self._buf, dtype=_np.float32).reshape(self._oshape)).copy()
        return output

    def forward_with_loss(self, x, labels, weights, loss_image_required, is_train=True):
        assert x.shape == self._ishape
        return self._loss_or_iteration_call(self._LIB.TCMPSForwardWithLoss, x, labels, weights, loss_image_required, is_train=is_train)

    def backward(self, g):
        assert g.shape == self._oshape
        g_array = MpsFloatArray(g)

        self._LIB.TCMPSBackward(self.handle, g_array.handle,
                                _ctypes.byref(self._buf_g))

        output = (_np.frombuffer(self._buf_g, dtype=_np.float32).reshape(self._ishape)).copy()
        return output

    def loss(self, x, labels, weights, loss_image_required):
        assert x.shape == self._oshape
        return self._loss_or_iteration_call(self._LIB.TCMPSLoss, x, labels, weights, loss_image_required)

    def _loss_or_iteration_call(self, lib_method, x, labels, weights, loss_image_required, is_train=None, async_batch_id=None):
        expected_label_shape = (self._oshape[:-1] + (1,))

        assert labels.shape == expected_label_shape
        assert weights.shape == expected_label_shape

        x_array = MpsFloatArray(x)
        labels_array = MpsFloatArray(labels)
        weights_array = MpsFloatArray(weights)
        loss_rq_bool = _ctypes.c_bool(loss_image_required)

        if async_batch_id is not None:
            batch_id = _ctypes.c_int(async_batch_id)
            if is_train is None:
                lib_method(self.handle, batch_id, x_array.handle,
                           labels_array.handle, weights_array.handle,
                           loss_rq_bool)
            else:
                lib_method(self.handle, batch_id, x_array.handle,
                           labels_array.handle, weights_array.handle,
                           loss_rq_bool, _ctypes.c_bool(is_train))
            return  # Async requests don't return anything immediately

        if is_train is None:
            lib_method(self.handle, x_array.handle, labels_array.handle,
                       weights_array.handle, loss_rq_bool,
                       _ctypes.byref(self._buf))
        else:
            lib_method(self.handle, x_array.handle, labels_array.handle,
                       weights_array.handle, loss_rq_bool,
                       _ctypes.c_bool(is_train), _ctypes.byref(self._buf))

        output = (_np.frombuffer(self._buf, dtype=_np.float32).reshape(self._oshape)).copy()
        return output

    def forward_backward(self, x, labels, weights, loss_image_required):
        assert x.shape == self._ishape
        return self._loss_or_iteration_call(self._LIB.TCMPSForwardBackward, x, labels, weights, loss_image_required)

    def get_loss_output(self):
        batch_size = self._ishape[0]
        loss_buff = (_ctypes.c_float * batch_size)()
        self._LIB.TCMPSGetLossImages(self.handle, _ctypes.byref(loss_buff))
        output = (_np.frombuffer(loss_buff, dtype=_np.float32)).copy()
        return output

    def begin_forward_batch(self, async_batch_id, x, labels, weights, loss_image_required, is_train=True):
        assert x.shape == self._ishape
        self._loss_or_iteration_call(self._LIB.TCMPSBeginForwardBatch, x, labels, weights, loss_image_required, is_train=is_train, async_batch_id=async_batch_id)

    def begin_forward_backward_batch(self, async_batch_id, x, labels, weights, loss_image_required):
        assert x.shape == self._ishape
        self._loss_or_iteration_call(self._LIB.TCMPSBeginForwardBackwardBatch, x, labels, weights, loss_image_required, async_batch_id=async_batch_id)

    def wait_for_batch(self, async_batch_id):
        batch_size = self._ishape[0]
        loss_buff = (_ctypes.c_float * batch_size)()
        self._LIB.TCMPSWaitForBatch(self.handle, _ctypes.c_int(async_batch_id),
                               _ctypes.byref(self._buf), _ctypes.byref(loss_buff))
        forward_output = (_np.frombuffer(self._buf, dtype=_np.float32).reshape(self._oshape)).copy()
        loss_output = (_np.frombuffer(loss_buff, dtype=_np.float32)).copy()
        return (forward_output, loss_output)

    def load(self, weights):
        weights_items, weights_name, weights_arr, weights_sz = _prepare_network_parameters(weights)
        self._LIB.TCMPSLoad(self.handle, weights_name, weights_arr, weights_sz, _ctypes.c_int32(len(weights_items)))

    def _num_params(self):
        num = _ctypes.c_int32(0)
        self._LIB.TCMPSNumParams(self.handle, _ctypes.byref(num))
        return num.value

    def export(self):
        iter_handle = _ctypes.c_void_p()
        status_code = self._LIB.TCMPSExport(self.handle,
                                            _ctypes.byref(iter_handle))
        assert status_code == 0
        return dict(MpsFloatArrayIterator(iter_handle))

    def cpu_update(self):
        self._LIB.TCMPSCpuUpdate(self.handle)

    def update(self):
        self._LIB.TCMPSUpdate(self.handle)

    def initalize_weights(self):
        args = self.export()
        for key, val in args.items():
            if key.endswith("weight"):
                args[key] = _xavier_init(val)
        self.load(args)