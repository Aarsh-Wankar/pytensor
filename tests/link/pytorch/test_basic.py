from collections.abc import Callable, Iterable
from functools import partial

import numpy as np
import pytest

import pytensor.tensor as pt
import pytensor.tensor.basic as ptb
from pytensor.compile.builders import OpFromGraph
from pytensor.compile.function import function
from pytensor.compile.mode import PYTORCH, Mode
from pytensor.compile.sharedvalue import shared
from pytensor.configdefaults import config
from pytensor.graph import RewriteDatabaseQuery
from pytensor.graph.basic import Apply, Variable
from pytensor.graph.fg import FunctionGraph
from pytensor.graph.op import Op
from pytensor.ifelse import ifelse
from pytensor.link.pytorch.linker import PytorchLinker
from pytensor.raise_op import CheckAndRaise
from pytensor.scalar import float64, int64
from pytensor.scalar.loop import ScalarLoop
from pytensor.tensor import alloc, arange, as_tensor, empty, expit, eye, softplus
from pytensor.tensor.elemwise import Elemwise
from pytensor.tensor.type import matrices, matrix, scalar, vector


torch = pytest.importorskip("torch")
torch_dispatch = pytest.importorskip("pytensor.link.pytorch.dispatch.basic")


optimizer = RewriteDatabaseQuery(
    # While we don't have a PyTorch implementation of Blockwise
    include=["local_useless_unbatched_blockwise"],
    exclude=PYTORCH._optimizer.exclude,
)
pytorch_mode = Mode(linker=PytorchLinker(), optimizer=optimizer)
py_mode = Mode(linker="py", optimizer=None)


def compare_pytorch_and_py(
    graph_inputs: Iterable[Variable],
    graph_outputs: Variable | Iterable[Variable],
    test_inputs: Iterable,
    assert_fn: Callable | None = None,
    pytorch_mode=pytorch_mode,
    py_mode=py_mode,
):
    """Function to compare python graph output and pytorch compiled output for testing equality

    Parameters
    ----------
    graph_inputs
        Symbolic inputs to the graph
    graph_outputs:
        Symbolic outputs of the graph
    test_inputs: iter
        Numerical inputs for testing the function graph
    assert_fn: func, opt
        Assert function used to check for equality between python and pytorch. If not
        provided uses np.testing.assert_allclose


    """
    if assert_fn is None:
        assert_fn = partial(np.testing.assert_allclose)

    if any(inp.owner is not None for inp in graph_inputs):
        raise ValueError("Inputs must be root variables")

    pytensor_torch_fn = function(graph_inputs, graph_outputs, mode=pytorch_mode)
    pytorch_res = pytensor_torch_fn(*test_inputs)

    pytensor_py_fn = function(graph_inputs, graph_outputs, mode=py_mode)
    py_res = pytensor_py_fn(*test_inputs)

    if isinstance(graph_outputs, list | tuple):
        for pytorch_res_i, py_res_i in zip(pytorch_res, py_res, strict=True):
            assert not isinstance(pytorch_res_i, torch.Tensor)
            assert_fn(pytorch_res_i, py_res_i)
    else:
        assert not isinstance(pytorch_res, torch.Tensor)
        assert_fn(pytorch_res, py_res)

    return pytensor_torch_fn, pytorch_res


@pytest.mark.parametrize("device", ["cpu", "cuda"])
def test_pytorch_FunctionGraph_once(device):
    if device == "cuda" and not torch.cuda.is_available():
        pytest.skip("CUDA is not available")
    """Make sure that an output is only computed once when it's referenced multiple times."""
    from pytensor.link.pytorch.dispatch import pytorch_funcify

    with torch.device(device):
        x = vector("x")
        y = vector("y")

        class TestOp(Op):
            def __init__(self):
                self.called = 0

            def make_node(self, *args):
                return Apply(self, list(args), [x.type() for x in args])

            def perform(self, inputs, outputs):
                for i, inp in enumerate(inputs):
                    outputs[i][0] = inp[0]

        @pytorch_funcify.register(TestOp)
        def pytorch_funcify_TestOp(op, **kwargs):
            def func(*args, op=op):
                op.called += 1
                for arg in args:
                    assert arg.device.type == device
                return list(args)

            return func

        op1 = TestOp()
        op2 = TestOp()

        q, r = op1(x, y)
        outs = op2(q + r, q + r)

        out_fg = FunctionGraph([x, y], outs, clone=False)
        assert len(out_fg.outputs) == 2

        out_torch = pytorch_funcify(out_fg)

        x_val = torch.tensor([1, 2]).to(getattr(torch, config.floatX))
        y_val = torch.tensor([2, 3]).to(getattr(torch, config.floatX))

        res = out_torch(x_val, y_val)

        for output in res:
            assert torch.equal(
                output, torch.tensor([3, 5]).to(getattr(torch, config.floatX))
            )

        assert len(res) == 2
        assert op1.called == 1
        assert op2.called == 1

        res = out_torch(x_val, y_val)

        for output in res:
            assert torch.equal(
                output, torch.tensor([3, 5]).to(getattr(torch, config.floatX))
            )

        assert len(res) == 2
        assert op1.called == 2
        assert op2.called == 2


@pytest.mark.parametrize("device", ["cpu", "cuda"])
def test_shared(device):
    if device == "cuda" and not torch.cuda.is_available():
        pytest.skip("CUDA is not available")
    with torch.device(device):
        a = shared(np.array([1, 2, 3], dtype=config.floatX))
        pytensor_torch_fn = function([], a, mode="PYTORCH")
        pytorch_res = pytensor_torch_fn()

        assert isinstance(pytorch_res, np.ndarray)
        assert isinstance(a.get_value(), np.ndarray)
        np.testing.assert_allclose(pytorch_res, a.get_value())

        pytensor_torch_fn = function([], a * 2, mode="PYTORCH")
        pytorch_res = pytensor_torch_fn()

        assert isinstance(pytorch_res, np.ndarray)
        assert isinstance(a.get_value(), np.ndarray)
        np.testing.assert_allclose(pytorch_res, a.get_value() * 2)

        new_a_value = np.array([3, 4, 5], dtype=config.floatX)
        a.set_value(new_a_value)

        pytorch_res = pytensor_torch_fn()
        assert isinstance(pytorch_res, np.ndarray)
        np.testing.assert_allclose(pytorch_res, new_a_value * 2)


@pytest.mark.parametrize("device", ["cpu", "cuda"])
def test_shared_updates(device):
    if device == "cuda" and not torch.cuda.is_available():
        pytest.skip("CUDA is not available")
    with torch.device(device):
        a = shared(0)

        pytensor_torch_fn = function([], a, updates={a: a + 1}, mode="PYTORCH")
        res1, res2 = pytensor_torch_fn(), pytensor_torch_fn()
        assert res1 == 0
        assert res2 == 1
        assert a.get_value() == 2
        assert isinstance(a.get_value(), np.ndarray)

        a.set_value(5)
        res1, res2 = pytensor_torch_fn(), pytensor_torch_fn()
        assert res1 == 5
        assert res2 == 6
        assert a.get_value() == 7
        assert isinstance(a.get_value(), np.ndarray)


def test_checkandraise():
    check_and_raise = CheckAndRaise(AssertionError, "testing")

    x = scalar("x")
    conds = (x > 0, x > 3)
    y = check_and_raise(x, *conds)

    y_fn = function([x], y, mode="PYTORCH")

    with pytest.raises(AssertionError, match="testing"):
        y_fn(0.0)
    assert y_fn(4).item() == 4


def test_alloc_and_empty():
    dim0 = as_tensor(5, dtype="int64")
    dim1 = scalar("dim1", dtype="int64")

    out = empty((dim0, dim1, 3), dtype="float32")
    fn = function([dim1], out, mode=pytorch_mode)
    res = fn(7)
    assert res.shape == (5, 7, 3)
    assert res.dtype == np.float32

    v = vector("v", shape=(3,), dtype="float64")
    out = alloc(v, dim0, dim1, 3)
    compare_pytorch_and_py(
        [v, dim1],
        [out],
        [np.array([1, 2, 3]), np.array(7)],
    )


def test_arange():
    start = scalar("start", dtype="int64")
    stop = scalar("stop", dtype="int64")
    step = scalar("step", dtype="int64")

    out = arange(start, stop, step, dtype="int16")

    compare_pytorch_and_py(
        [start, stop, step],
        [out],
        [np.array(1), np.array(10), np.array(2)],
    )


def test_pytorch_Join():
    a = matrix("a")
    b = matrix("b")

    x = ptb.join(0, a, b)

    compare_pytorch_and_py(
        [a, b],
        [x],
        [
            np.c_[[1.0, 2.0, 3.0]].astype(config.floatX),
            np.c_[[4.0, 5.0, 6.0]].astype(config.floatX),
        ],
    )
    compare_pytorch_and_py(
        [a, b],
        [x],
        [
            np.c_[[1.0, 2.0, 3.0]].astype(config.floatX),
            np.c_[[4.0, 5.0]].astype(config.floatX),
        ],
    )

    x = ptb.join(1, a, b)

    compare_pytorch_and_py(
        [a, b],
        [x],
        [
            np.c_[[1.0, 2.0, 3.0]].astype(config.floatX),
            np.c_[[4.0, 5.0, 6.0]].astype(config.floatX),
        ],
    )
    compare_pytorch_and_py(
        [a, b],
        [x],
        [
            np.c_[[1.0, 2.0], [3.0, 4.0]].astype(config.floatX),
            np.c_[[5.0, 6.0]].astype(config.floatX),
        ],
    )


@pytest.mark.parametrize(
    "dtype",
    ["int64", config.floatX],
)
def test_eye(dtype):
    N = scalar("N", dtype="int64")
    M = scalar("M", dtype="int64")
    k = scalar("k", dtype="int64")

    out = eye(N, M, k, dtype=dtype)

    fn = function([N, M, k], out, mode=pytorch_mode)

    for _N in range(1, 6):
        for _M in range(1, 6):
            for _k in list(range(_M + 2)) + [-x for x in range(1, _N + 2)]:
                np.testing.assert_array_equal(fn(_N, _M, _k), np.eye(_N, _M, _k))


def test_pytorch_MakeVector():
    x = ptb.make_vector(1, 2, 3)

    compare_pytorch_and_py([], [x], [])


def test_pytorch_ifelse():
    p1_vals = np.r_[1, 2, 3]
    p2_vals = np.r_[-1, -2, -3]

    a = scalar("a")
    x = ifelse(a < 0.5, tuple(np.r_[p1_vals, p2_vals]), tuple(np.r_[p2_vals, p1_vals]))

    compare_pytorch_and_py([a], x, np.array([0.2], dtype=config.floatX))

    a = scalar("a")
    x = ifelse(a < 0.4, tuple(np.r_[p1_vals, p2_vals]), tuple(np.r_[p2_vals, p1_vals]))

    compare_pytorch_and_py([a], x, np.array([0.5], dtype=config.floatX))


def test_pytorch_OpFromGraph():
    x, y, z = matrices("xyz")
    ofg_1 = OpFromGraph([x, y], [x + y])
    ofg_2 = OpFromGraph([x, y], [x * y, x - y])

    o1, o2 = ofg_2(y, z)
    out = ofg_1(x, o1) / o2

    xv = np.ones((2, 2), dtype=config.floatX)
    yv = np.ones((2, 2), dtype=config.floatX) * 3
    zv = np.ones((2, 2), dtype=config.floatX) * 5

    compare_pytorch_and_py([x, y, z], [out], [xv, yv, zv])


def test_pytorch_link_references():
    import pytensor.link.utils as m

    class BasicOp(Op):
        def __init__(self):
            super().__init__()

        def make_node(self, *x):
            return Apply(self, list(x), [xi.type() for xi in x])

        def perform(self, *_):
            raise RuntimeError("In perform")

    @torch_dispatch.pytorch_funcify.register(BasicOp)
    def fn(op, node, **kwargs):
        def inner_fn(x):
            assert "inner_fn" in dir(m), "not available during dispatch"
            return x

        return inner_fn

    x = vector("x")
    op = BasicOp()
    out = op(x)

    f = function([x], out, mode="PYTORCH")
    f(torch.ones(3))
    assert "inner_fn" not in dir(m), "function call reference leaked"


def test_pytorch_scipy():
    x = vector("a", shape=(3,))
    out = expit(x)
    compare_pytorch_and_py([x], [out], [np.random.rand(3)])


def test_pytorch_softplus():
    x = vector("a", shape=(3,))
    out = softplus(x)
    compare_pytorch_and_py([x], [out], [np.random.rand(3)])


def test_ScalarLoop():
    n_steps = int64("n_steps")
    x0 = float64("x0")
    const = float64("const")
    x = x0 + const

    op = ScalarLoop(init=[x0], constant=[const], update=[x])
    x = op(n_steps, x0, const)

    fn = function([n_steps, x0, const], x, mode=pytorch_mode)
    np.testing.assert_allclose(fn(5, 0, 1), 5)
    np.testing.assert_allclose(fn(5, 0, 2), 10)
    np.testing.assert_allclose(fn(4, 3, -1), -1)


def test_ScalarLoop_while():
    n_steps = int64("n_steps")
    x0 = float64("x0")
    x = x0 + 1
    until = x >= 10

    op = ScalarLoop(init=[x0], update=[x], until=until)
    fn = function([n_steps, x0], op(n_steps, x0), mode=pytorch_mode)
    for res, expected in zip(
        [fn(n_steps=20, x0=0), fn(n_steps=20, x0=1), fn(n_steps=5, x0=1)],
        [[10, True], [10, True], [6, False]],
        strict=True,
    ):
        np.testing.assert_allclose(res[0], np.array(expected[0]))
        np.testing.assert_allclose(res[1], np.array(expected[1]))


def test_ScalarLoop_Elemwise_single_carries():
    n_steps = int64("n_steps")
    x0 = float64("x0")
    x = x0 * 2
    until = x >= 10

    scalarop = ScalarLoop(init=[x0], update=[x], until=until)
    op = Elemwise(scalarop)

    n_steps = pt.scalar("n_steps", dtype="int32")
    x0 = pt.vector("x0", dtype="float32")
    state, done = op(n_steps, x0)

    args = [
        np.array(10).astype("int32"),
        np.arange(0, 5).astype("float32"),
    ]
    compare_pytorch_and_py(
        [n_steps, x0],
        [state, done],
        args,
        assert_fn=partial(np.testing.assert_allclose, rtol=1e-6),
    )


def test_ScalarLoop_Elemwise_multi_carries():
    n_steps = int64("n_steps")
    x0 = float64("x0")
    x1 = float64("x1")
    x = x0 * 2
    x1_n = x1 * 3
    until = x >= 10

    scalarop = ScalarLoop(init=[x0, x1], update=[x, x1_n], until=until)
    op = Elemwise(scalarop)

    n_steps = pt.scalar("n_steps", dtype="int32")
    x0 = pt.vector("x0", dtype="float32")
    x1 = pt.tensor("c0", dtype="float32", shape=(7, 3, 1))
    *states, done = op(n_steps, x0, x1)

    args = [
        np.array(10).astype("int32"),
        np.arange(0, 5).astype("float32"),
        np.random.rand(7, 3, 1).astype("float32"),
    ]
    compare_pytorch_and_py(
        [n_steps, x0, x1],
        [*states, done],
        args,
        assert_fn=partial(np.testing.assert_allclose, rtol=1e-6),
    )


rng = np.random.default_rng(42849)


@pytest.mark.parametrize(
    "n_splits, axis, values, sizes",
    [
        (
            0,
            0,
            rng.normal(size=20).astype(config.floatX),
            [],
        ),
        (
            5,
            0,
            rng.normal(size=5).astype(config.floatX),
            rng.multinomial(5, np.ones(5) / 5),
        ),
        (
            5,
            0,
            rng.normal(size=10).astype(config.floatX),
            rng.multinomial(10, np.ones(5) / 5),
        ),
        (
            5,
            -1,
            rng.normal(size=(11, 7)).astype(config.floatX),
            rng.multinomial(7, np.ones(5) / 5),
        ),
        (
            5,
            -2,
            rng.normal(size=(11, 7)).astype(config.floatX),
            rng.multinomial(11, np.ones(5) / 5),
        ),
    ],
)
def test_Split(n_splits, axis, values, sizes):
    i = pt.tensor("i", shape=values.shape, dtype=config.floatX)
    s = pt.vector("s", dtype="int64")
    g = pt.split(i, s, n_splits, axis=axis)
    assert len(g) == n_splits
    if n_splits == 0:
        return

    compare_pytorch_and_py([i, s], g, [values, sizes])
