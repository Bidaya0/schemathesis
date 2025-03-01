from contextlib import contextmanager
from functools import partial
from typing import Any, Callable, Dict, Generator, List, Optional, Tuple, Type, TypeVar, cast

import pytest
from _pytest import fixtures, nodes
from _pytest.config import hookimpl
from _pytest.fixtures import FuncFixtureInfo
from _pytest.nodes import Node
from _pytest.python import Class, Function, FunctionDefinition, Metafunc, Module, PyCollector
from hypothesis import reporting
from hypothesis.errors import InvalidArgument
from hypothesis_jsonschema._canonicalise import HypothesisRefResolutionError

from .._hypothesis import create_test
from ..constants import IS_PYTEST_ABOVE_7, IS_PYTEST_ABOVE_54, RECURSIVE_REFERENCE_ERROR_MESSAGE
from ..exceptions import InvalidSchema, SkipTest
from ..models import APIOperation
from ..utils import (
    PARAMETRIZE_MARKER,
    Ok,
    Result,
    fail_on_no_matches,
    get_given_args,
    get_given_kwargs,
    is_given_applied,
    is_schemathesis_test,
    merge_given_args,
    validate_given_args,
)

T = TypeVar("T", bound=Node)


def create(cls: Type[T], *args: Any, **kwargs: Any) -> T:
    if IS_PYTEST_ABOVE_54:
        return cls.from_parent(*args, **kwargs)  # type: ignore
    return cls(*args, **kwargs)


class SchemathesisFunction(Function):  # pylint: disable=too-many-ancestors
    def __init__(
        self,
        *args: Any,
        test_func: Callable,
        test_name: Optional[str] = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self.test_function = test_func
        self.test_name = test_name

    if not IS_PYTEST_ABOVE_7:
        # On pytest 7, `self.obj` is already `partial`
        def _getobj(self) -> partial:
            """Tests defined as methods require `self` as the first argument.

            This method is called only for this case.
            """
            return partial(self.obj, self.parent.obj)  # type: ignore


class SchemathesisCase(PyCollector):
    def __init__(self, test_function: Callable, *args: Any, **kwargs: Any) -> None:
        self.given_kwargs: Optional[Dict[str, Any]]
        given_args = get_given_args(test_function)
        given_kwargs = get_given_kwargs(test_function)

        def _init_with_valid_test(_test_function: Callable, _args: Tuple, _kwargs: Dict[str, Any]) -> None:
            self.test_function = _test_function
            self.is_invalid_test = False
            self.given_kwargs = merge_given_args(test_function, _args, _kwargs)

        if is_given_applied(test_function):
            failing_test = validate_given_args(test_function, given_args, given_kwargs)
            if failing_test is not None:
                self.test_function = failing_test
                self.is_invalid_test = True
                self.given_kwargs = None
            else:
                _init_with_valid_test(test_function, given_args, given_kwargs)
        else:
            _init_with_valid_test(test_function, given_args, given_kwargs)
        self.schemathesis_case = getattr(test_function, PARAMETRIZE_MARKER)
        super().__init__(*args, **kwargs)

    def _get_test_name(self, operation: APIOperation) -> str:
        return f"{self.name}[{operation.verbose_name}]"

    def _gen_items(self, result: Result[APIOperation, InvalidSchema]) -> Generator[SchemathesisFunction, None, None]:
        """Generate all tests for the given API operation.

        Could produce more than one test item if
        parametrization is applied via ``pytest.mark.parametrize`` or ``pytest_generate_tests``.

        This implementation is based on the original one in pytest, but with slight adjustments
        to produce tests out of hypothesis ones.
        """
        if isinstance(result, Ok):
            operation = result.ok()
            if self.is_invalid_test:
                funcobj = self.test_function
            else:
                funcobj = create_test(
                    operation=operation,
                    test=self.test_function,
                    _given_kwargs=self.given_kwargs,
                    data_generation_methods=self.schemathesis_case.data_generation_methods,
                )
            name = self._get_test_name(operation)
        else:
            error = result.err()
            funcobj = error.as_failing_test_function()
            name = self.name
            # `full_path` is always available in this case
            if error.method:
                name += f"[{error.method.upper()} {error.full_path}]"
            else:
                name += f"[{error.full_path}]"

        cls = self._get_class_parent()
        definition: FunctionDefinition = create(FunctionDefinition, name=self.name, parent=self.parent, callobj=funcobj)
        fixturemanager = self.session._fixturemanager
        fixtureinfo = fixturemanager.getfixtureinfo(definition, funcobj, cls)

        metafunc = self._parametrize(cls, definition, fixtureinfo)

        if isinstance(self.parent, Class):
            # On pytest 7, Class collects the test methods directly, therefore
            funcobj = partial(funcobj, self.parent.obj)

        if not metafunc._calls:
            yield create(
                SchemathesisFunction,
                name=name,
                parent=self.parent,
                callobj=funcobj,
                fixtureinfo=fixtureinfo,
                test_func=self.test_function,
                originalname=self.name,
            )
        else:
            fixtures.add_funcarg_pseudo_fixture_def(self.parent, metafunc, fixturemanager)  # type: ignore[arg-type]
            fixtureinfo.prune_dependency_tree()
            for callspec in metafunc._calls:
                subname = f"{name}[{callspec.id}]"
                yield create(
                    SchemathesisFunction,
                    name=subname,
                    parent=self.parent,
                    callspec=callspec,
                    callobj=funcobj,
                    fixtureinfo=fixtureinfo,
                    keywords={callspec.id: True},
                    originalname=name,
                    test_func=self.test_function,
                )

    def _get_class_parent(self) -> Optional[Type]:
        clscol = self.getparent(Class)
        return clscol.obj if clscol else None

    def _parametrize(
        self, cls: Optional[Type], definition: FunctionDefinition, fixtureinfo: FuncFixtureInfo
    ) -> Metafunc:
        parent = self.getparent(Module)
        module = parent.obj if parent is not None else parent
        kwargs = {"cls": cls, "module": module}
        if IS_PYTEST_ABOVE_7:
            # Avoiding `Metafunc` is quite problematic for now, as there are quite a lot of internals we rely on
            kwargs["_ispytest"] = True
        metafunc = Metafunc(definition, fixtureinfo, self.config, **kwargs)
        methods = []
        if hasattr(module, "pytest_generate_tests"):
            methods.append(module.pytest_generate_tests)
        if hasattr(cls, "pytest_generate_tests"):
            cls = cast(Type, cls)
            methods.append(cls().pytest_generate_tests)
        self.ihook.pytest_generate_tests.call_extra(methods, {"metafunc": metafunc})
        return metafunc

    def collect(self) -> List[Function]:  # type: ignore
        """Generate different test items for all API operations available in the given schema."""
        try:
            items = [
                item for operation in self.schemathesis_case.get_all_operations() for item in self._gen_items(operation)
            ]
            if not items:
                fail_on_no_matches(self.nodeid)
            return items
        except Exception:
            pytest.fail("Error during collection")


@hookimpl(hookwrapper=True)  # type:ignore # pragma: no mutate
def pytest_pycollect_makeitem(collector: nodes.Collector, name: str, obj: Any) -> Generator[None, Any, None]:
    """Switch to a different collector if the test is parametrized marked by schemathesis."""
    outcome = yield
    if is_schemathesis_test(obj):
        outcome.force_result(create(SchemathesisCase, parent=collector, test_function=obj, name=name))
    else:
        outcome.get_result()


IGNORED_HYPOTHESIS_OUTPUT = ("Falsifying example",)


def _should_ignore_entry(value: str) -> bool:
    return value.startswith(IGNORED_HYPOTHESIS_OUTPUT)


def hypothesis_reporter(value: str) -> None:
    if _should_ignore_entry(value):
        return
    reporting.default(value)


@contextmanager
def skip_unnecessary_hypothesis_output() -> Generator:
    """Avoid printing Hypothesis output that is not necessary in Schemathesis' pytest plugin."""
    with reporting.with_reporter(hypothesis_reporter):  # type: ignore
        yield


@hookimpl(hookwrapper=True)  # pragma: no mutate
def pytest_pyfunc_call(pyfuncitem):  # type:ignore
    """It is possible to have a Hypothesis exception in runtime.

    For example - kwargs validation is failed for some strategy.
    """
    if isinstance(pyfuncitem, SchemathesisFunction):
        with skip_unnecessary_hypothesis_output():
            outcome = yield
        try:
            outcome.get_result()
        except InvalidArgument as exc:
            raise InvalidSchema(exc.args[0]) from None
        except HypothesisRefResolutionError:
            pytest.skip(RECURSIVE_REFERENCE_ERROR_MESSAGE)
        except SkipTest as exc:
            pytest.skip(exc.args[0])
        except Exception as exc:
            if hasattr(exc, "__notes__"):
                exc.__notes__ = [note for note in exc.__notes__ if not _should_ignore_entry(note)]  # type: ignore
            raise
    else:
        outcome = yield
        outcome.get_result()
