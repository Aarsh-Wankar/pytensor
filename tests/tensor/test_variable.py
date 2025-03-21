import re
from copy import copy

import numpy as np
import pytest
from numpy.testing import assert_array_equal, assert_equal, assert_string_equal

import pytensor
import tests.unittest_tools as utt
from pytensor.compile import DeepCopyOp
from pytensor.compile.mode import get_default_mode
from pytensor.graph.basic import Constant, equal_computations
from pytensor.tensor import get_vector_length
from pytensor.tensor.basic import constant
from pytensor.tensor.elemwise import DimShuffle
from pytensor.tensor.math import dot, eq, matmul
from pytensor.tensor.shape import Shape
from pytensor.tensor.subtensor import (
    AdvancedSubtensor,
    Subtensor,
    inc_subtensor,
    set_subtensor,
)
from pytensor.tensor.type import (
    TensorType,
    cscalar,
    dmatrix,
    dscalar,
    dvector,
    iscalar,
    ivector,
    matrices,
    matrix,
    scalar,
    tensor3,
)
from pytensor.tensor.type_other import MakeSlice, NoneConst
from pytensor.tensor.variable import (
    DenseTensorConstant,
    DenseTensorVariable,
    TensorConstant,
    TensorVariable,
)
from tests.tensor.utils import random


pytestmark = pytest.mark.filterwarnings("error")


@pytest.mark.parametrize(
    "fct, value",
    [
        (np.arccos, 0.5),
        (np.arccosh, 1.0),
        (np.arcsin, 0.5),
        (np.arcsinh, 0.5),
        (np.arctan, 0.5),
        (np.arctanh, 0.5),
        (np.cos, 0.5),
        (np.cosh, 0.5),
        (np.deg2rad, 0.5),
        (np.exp, 0.5),
        (np.exp2, 0.5),
        (np.expm1, 0.5),
        (np.log, 0.5),
        (np.log10, 0.5),
        (np.log1p, 0.5),
        (np.log2, 0.5),
        (np.rad2deg, 0.5),
        (np.sin, 0.5),
        (np.sinh, 0.5),
        (np.sqrt, 0.5),
        (np.tan, 0.5),
        (np.tanh, 0.5),
    ],
)
def test_numpy_method(fct, value):
    x = dscalar("x")
    y = fct(x)
    f = pytensor.function([x], y)
    utt.assert_allclose(np.nan_to_num(f(value)), np.nan_to_num(fct(value)))


def test_dot_method():
    X = dmatrix("X")
    y = dvector("y")

    res = X.dot(y)
    exp_res = dot(X, y)
    assert equal_computations([res], [exp_res])

    # This doesn't work. Numpy calls TensorVariable.__rmul__ at some point and everything is messed up
    X_val = np.arange(2 * 3).reshape((2, 3))
    res = X_val.dot(y)
    exp_res = dot(X_val, y)
    with pytest.raises(AssertionError):
        assert equal_computations([res], [exp_res])


def test_infix_matmul_method():
    X = dmatrix("X")
    y = dvector("y")

    res = X @ y
    exp_res = matmul(X, y)
    assert equal_computations([res], [exp_res])

    X_val = np.arange(2 * 3).reshape((2, 3))
    res = X_val @ y
    exp_res = matmul(X_val, y)
    assert equal_computations([res], [exp_res])

    y_val = np.arange(3)
    res = X @ y_val
    exp_res = matmul(X, y_val)
    assert equal_computations([res], [exp_res])


def test_empty_list_indexing():
    ynp = np.zeros((2, 2))[:, []]
    znp = np.zeros((2, 2))[:, ()]
    data = [[0, 0], [0, 0]]
    x = dmatrix("x")
    y = x[:, []]
    z = x[:, ()]
    fy = pytensor.function([x], y)
    fz = pytensor.function([x], z)
    assert_equal(fy(data).shape, ynp.shape)
    assert_equal(fz(data).shape, znp.shape)


def test_copy():
    x = dmatrix("x")
    data = np.random.random((5, 5))
    y = x.copy(name="y")
    f = pytensor.function([x], y)
    assert_equal(f(data), data)
    assert_string_equal(y.name, "y")


def test__getitem__Subtensor():
    # Make sure we get `Subtensor`s for basic indexing operations
    x = matrix("x")
    i = iscalar("i")

    z = x[i]
    op_types = [type(node.op) for node in pytensor.graph.basic.io_toposort([x, i], [z])]
    assert op_types[-1] == Subtensor

    # This should ultimately do nothing (i.e. just return `x`)
    z = x[()]
    assert len(z.owner.op.idx_list) == 0
    # assert z is x

    # This is a poorly placed optimization that produces a `DimShuffle`
    # It lands in the `full_slices` condition in
    # `_tensor_py_operators.__getitem__`
    z = x[..., None]
    op_types = [type(node.op) for node in pytensor.graph.basic.io_toposort([x, i], [z])]
    assert all(op_type == DimShuffle for op_type in op_types)

    z = x[None, :, None, :]
    op_types = [type(node.op) for node in pytensor.graph.basic.io_toposort([x, i], [z])]
    assert all(op_type == DimShuffle for op_type in op_types)

    # This one lands in the non-`full_slices` condition in
    # `_tensor_py_operators.__getitem__`
    z = x[:i, :, None]
    op_types = [type(node.op) for node in pytensor.graph.basic.io_toposort([x, i], [z])]
    assert op_types[1:] == [DimShuffle, Subtensor]

    z = x[:]
    op_types = [type(node.op) for node in pytensor.graph.basic.io_toposort([x, i], [z])]
    assert op_types[-1] == Subtensor

    z = x[..., :]
    op_types = [type(node.op) for node in pytensor.graph.basic.io_toposort([x, i], [z])]
    assert op_types[-1] == Subtensor

    z = x[..., i, :]
    op_types = [type(node.op) for node in pytensor.graph.basic.io_toposort([x, i], [z])]
    assert op_types[-1] == Subtensor


def test__getitem__AdvancedSubtensor_bool():
    x = matrix("x")
    i = TensorType("bool", shape=(None, None))("i")

    z = x[i]
    op_types = [type(node.op) for node in pytensor.graph.basic.io_toposort([x, i], [z])]
    assert op_types[-1] == AdvancedSubtensor

    i = TensorType("bool", shape=(None,))("i")
    z = x[:, i]
    op_types = [type(node.op) for node in pytensor.graph.basic.io_toposort([x, i], [z])]
    assert op_types[-1] == AdvancedSubtensor

    i = TensorType("bool", shape=(None,))("i")
    z = x[..., i]
    op_types = [type(node.op) for node in pytensor.graph.basic.io_toposort([x, i], [z])]
    assert op_types[-1] == AdvancedSubtensor

    with pytest.raises(TypeError):
        z = x[[True, False], i]

    z = x[ivector("b"), i]
    op_types = [type(node.op) for node in pytensor.graph.basic.io_toposort([x, i], [z])]
    assert op_types[-1] == AdvancedSubtensor


def test__getitem__AdvancedSubtensor():
    # Make sure we get `AdvancedSubtensor`s for basic indexing operations
    x = matrix("x")
    i = ivector("i")

    # This is a `__getitem__` call that's redirected to `_tensor_py_operators.take`
    z = x[i]
    op_types = [type(node.op) for node in pytensor.graph.basic.io_toposort([x, i], [z])]
    assert op_types[-1] == AdvancedSubtensor

    # This should index nothing (i.e. return an empty copy of `x`)
    # We check that the index is empty
    z = x[[]]
    op_types = [type(node.op) for node in pytensor.graph.basic.io_toposort([x, i], [z])]
    assert op_types == [AdvancedSubtensor]
    assert isinstance(z.owner.inputs[1], TensorConstant)

    z = x[:, i]
    op_types = [type(node.op) for node in pytensor.graph.basic.io_toposort([x, i], [z])]
    assert op_types == [MakeSlice, AdvancedSubtensor]

    z = x[..., i, None]
    op_types = [type(node.op) for node in pytensor.graph.basic.io_toposort([x, i], [z])]
    assert op_types == [MakeSlice, AdvancedSubtensor]

    z = x[i, None]
    op_types = [type(node.op) for node in pytensor.graph.basic.io_toposort([x, i], [z])]
    assert op_types[-1] == AdvancedSubtensor


def test_print_constant():
    c = pytensor.tensor.constant(1, name="const")
    assert str(c) == "const{1}"
    d = pytensor.tensor.constant(1)
    assert str(d) == "1"


@pytest.mark.parametrize(
    "x, indices, new_order",
    [
        (tensor3(), (np.newaxis, slice(None), np.newaxis), ("x", 0, "x", 1, 2)),
        (cscalar(), (np.newaxis,), ("x",)),
        (cscalar(), (NoneConst,), ("x",)),
        (matrix(), (np.newaxis,), ("x", 0, 1)),
        (matrix(), (np.newaxis, np.newaxis), ("x", "x", 0, 1)),
        (matrix(), (np.newaxis, slice(None)), ("x", 0, 1)),
        (matrix(), (np.newaxis, slice(None), slice(None)), ("x", 0, 1)),
        (matrix(), (np.newaxis, np.newaxis, slice(None)), ("x", "x", 0, 1)),
        (matrix(), (slice(None), np.newaxis), (0, "x", 1)),
        (matrix(), (slice(None), slice(None), np.newaxis), (0, 1, "x")),
        (
            matrix(),
            (np.newaxis, slice(None), np.newaxis, slice(None), np.newaxis),
            ("x", 0, "x", 1, "x"),
        ),
    ],
)
def test__getitem__newaxis(x, indices, new_order):
    res = x[indices]
    assert isinstance(res.owner.op, DimShuffle)
    assert res.broadcastable == tuple(i == "x" for i in new_order)
    assert res.owner.op.new_order == new_order


def test_fixed_shape_variable_basic():
    x = TensorVariable(TensorType("int64", shape=(4,)), None)
    assert x.type.shape == (4,)
    assert isinstance(x.shape.owner.op, Shape)

    shape_fn = pytensor.function([x], x.shape)
    opt_shape = shape_fn.maker.fgraph.outputs[0]
    assert isinstance(opt_shape.owner.op, DeepCopyOp)
    assert isinstance(opt_shape.owner.inputs[0], Constant)
    assert np.array_equal(opt_shape.owner.inputs[0].data, (4,))

    x = TensorConstant(
        TensorType("int64", shape=(None, None)), np.array([[1, 2], [2, 3]])
    )
    assert x.type.shape == (2, 2)

    with pytest.raises(ValueError):
        TensorConstant(TensorType("int64", shape=(1, None)), np.array([[1, 2], [2, 3]]))


def test_get_vector_length():
    x = TensorVariable(TensorType("int64", shape=(4,)), None)
    res = get_vector_length(x)
    assert res == 4

    x = TensorVariable(TensorType("int64", shape=(None,)), None)
    with pytest.raises(ValueError):
        get_vector_length(x)


def test_dense_types():
    x = matrix()
    assert isinstance(x, DenseTensorVariable)
    assert not isinstance(x, DenseTensorConstant)

    x = constant(1)
    assert not isinstance(x, DenseTensorVariable)
    assert isinstance(x, DenseTensorConstant)


class TestTensorConstantSignature:
    vals = [
        [np.nan, np.inf, 0, 1],
        [np.nan, np.inf, -np.inf, 1],
        [0, np.inf, -np.inf, 1],
        [0, 3, -np.inf, 1],
        [0, 3, np.inf, 1],
        [np.nan, 3, 4, 1],
        [0, 3, 4, 1],
        np.nan,
        np.inf,
        -np.inf,
        0,
        1,
    ]

    @pytest.mark.parametrize("val_1", vals)
    @pytest.mark.parametrize("val_2", vals)
    def test_nan_inf_constant_signature(self, val_1, val_2):
        # Test that the signature of a constant tensor containing NaN and Inf
        # values is correct.
        # We verify that signatures of two rows i, j in the matrix above are
        # equal if and only if i == j.
        x = constant(val_1)
        y = constant(val_2)
        assert (x.signature() == y.signature()) == (val_1 is val_2)

    def test_nan_nan(self):
        # Also test that nan !=0 and nan != nan.
        x = scalar()
        mode = get_default_mode()
        if isinstance(mode, pytensor.compile.debugmode.DebugMode):
            # Disable the check preventing usage of NaN / Inf values.
            # We first do a copy of the mode to avoid side effects on other tests.
            mode = copy(mode)
            mode.check_isfinite = False
        f = pytensor.function([x], eq(x, np.nan), mode=mode)

        assert f(0) == 0
        assert f(np.nan) == 0

    def test_empty_hash(self):
        x = constant(np.array([], dtype=np.int64))
        y = constant(np.array([], dtype=np.int64))

        x_sig = x.signature()
        y_sig = y.signature()

        assert hash(x_sig) == hash(y_sig)


class TestTensorInstanceMethods:
    def setup_method(self):
        self.vars = matrices("X", "Y")
        self.vals = [
            m.astype(pytensor.config.floatX) for m in [random(2, 2), random(2, 2)]
        ]

    def test_repeat(self):
        X, _ = self.vars
        x, _ = self.vals
        assert_array_equal(X.repeat(2).eval({X: x}), x.repeat(2))

    def test_trace(self):
        X, _ = self.vars
        x, _ = self.vals
        with pytest.warns(FutureWarning):
            assert_array_equal(X.trace().eval({X: x}), x.trace())

    def test_ravel(self):
        X, _ = self.vars
        x, _ = self.vals
        assert_array_equal(X.ravel().eval({X: x}), x.ravel())

    def test_diagonal(self):
        X, _ = self.vars
        x, _ = self.vals
        assert_array_equal(X.diagonal().eval({X: x}), x.diagonal())
        assert_array_equal(X.diagonal(1).eval({X: x}), x.diagonal(1))
        assert_array_equal(X.diagonal(-1).eval({X: x}), x.diagonal(-1))
        for offset, axis1, axis2 in [(1, 0, 1), (-1, 0, 1), (0, 1, 0), (-2, 1, 0)]:
            assert_array_equal(
                X.diagonal(offset, axis1, axis2).eval({X: x}),
                x.diagonal(offset, axis1, axis2),
            )

    def test_take(self):
        X, _ = self.vars
        x, _ = self.vals
        indices = [1, 0, 3]
        assert_array_equal(X.take(indices).eval({X: x}), x.take(indices))
        indices = [1, 0, 1]
        assert_array_equal(X.take(indices, 1).eval({X: x}), x.take(indices, 1))
        indices = np.array([-10, 5, 12], dtype="int32")
        assert_array_equal(
            X.take(indices, 1, mode="wrap").eval({X: x}),
            x.take(indices, 1, mode="wrap"),
        )
        assert_array_equal(
            X.take(indices, -1, mode="wrap").eval({X: x}),
            x.take(indices, -1, mode="wrap"),
        )
        assert_array_equal(
            X.take(indices, 1, mode="clip").eval({X: x}),
            x.take(indices, 1, mode="clip"),
        )
        assert_array_equal(
            X.take(indices, -1, mode="clip").eval({X: x}),
            x.take(indices, -1, mode="clip"),
        )
        # Test error handling
        with pytest.raises(IndexError):
            X.take(indices).eval({X: x})
        with pytest.raises(IndexError):
            (2 * X.take(indices)).eval({X: x})
        with pytest.raises(TypeError):
            X.take([0.0])
        indices = [[1, 0, 1], [0, 1, 1]]
        assert_array_equal(X.take(indices, 1).eval({X: x}), x.take(indices, 1))
        # Test equivalent advanced indexing
        assert_array_equal(X[:, indices].eval({X: x}), x[:, indices])

    def test_set_inc(self):
        x = matrix("x")
        idx = [0]
        y = 5

        assert equal_computations([x[:, idx].set(y)], [set_subtensor(x[:, idx], y)])
        assert equal_computations([x[:, idx].inc(y)], [inc_subtensor(x[:, idx], y)])

    def test_set_item_error(self):
        x = matrix("x")

        msg = re.escape("Use the output of `x[idx].set` or `x[idx].inc` instead.")
        with pytest.raises(TypeError, match=msg):
            x[0] = 5
        with pytest.raises(TypeError, match=msg):
            x[0] += 5

    def test_transpose(self):
        X, _ = self.vars
        x, _ = self.vals

        # Turn (2,2) -> (1,2)
        X, x = X[1:, :], x[1:, :]

        assert_array_equal(X.transpose(0, 1).eval({X: x}), x.transpose(0, 1))
        assert_array_equal(X.transpose(1, 0).eval({X: x}), x.transpose(1, 0))

        # Test handing in tuples, lists and np.arrays
        equal_computations([X.transpose((1, 0))], [X.transpose(1, 0)])
        equal_computations([X.transpose([1, 0])], [X.transpose(1, 0)])
        equal_computations([X.transpose(np.array([1, 0]))], [X.transpose(1, 0)])


def test_deprecated_import():
    with pytest.warns(
        DeprecationWarning,
        match="The module 'pytensor.tensor.var' has been deprecated.",
    ):
        import pytensor.tensor.var as _var

        # Make sure the deprecated import provides access to 'variable' module
        assert hasattr(_var, "TensorVariable")
        assert hasattr(_var, "TensorConstant")
