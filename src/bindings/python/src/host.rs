//! Await Python objects on the caller's asyncio task, without a second scheduler.
use pyo3::{
    exceptions::{PyAttributeError, PyGeneratorExit, PyRuntimeError, PyStopIteration, PyTypeError},
    prelude::*,
    types::PyTuple,
};
use std::{
    cell::RefCell,
    future::Future,
    pin::Pin,
    sync::{Arc, Mutex},
    task::{Context, Poll, Waker},
};

type Output = PyResult<Py<PyAny>>;
type ResultSlot = Arc<Mutex<Option<Output>>>;
struct Request {
    awaitable: Py<PyAny>,
    result: ResultSlot,
}
thread_local! { static REQUEST: RefCell<Option<Request>> = const { RefCell::new(None) }; }
struct AwaitPython {
    awaitable: Option<Py<PyAny>>,
    result: ResultSlot,
}
impl Future for AwaitPython {
    type Output = Output;
    fn poll(mut self: Pin<&mut Self>, _: &mut Context<'_>) -> Poll<Output> {
        if let Some(result) = self.result.lock().unwrap_or_else(|e| e.into_inner()).take() {
            return Poll::Ready(result);
        }
        if let Some(awaitable) = self.awaitable.take() {
            REQUEST.with(|slot| {
                *slot.borrow_mut() = Some(Request {
                    awaitable,
                    result: self.result.clone(),
                })
            });
        }
        Poll::Pending
    }
}
pub async fn await_py(awaitable: Py<PyAny>) -> Output {
    AwaitPython {
        awaitable: Some(awaitable),
        result: Arc::new(Mutex::new(None)),
    }
    .await
}
pub fn coroutine(py: Python<'_>, future: impl Future<Output = Output> + Send + 'static) -> Output {
    Ok(Py::new(
        py,
        HostCoroutine {
            future: Mutex::new(Some(Box::pin(future))),
            awaiting: None,
            started: false,
        },
    )?
    .into_any())
}
struct Awaiting {
    iterator: Py<PyAny>,
    result: ResultSlot,
}
#[pyclass(module = "agentchat._native")]
pub struct HostCoroutine {
    future: Mutex<Option<Pin<Box<dyn Future<Output = Output> + Send>>>>,
    awaiting: Option<Awaiting>,
    started: bool,
}
fn thrown_error(py: Python<'_>, args: &Bound<'_, PyTuple>) -> PyResult<PyErr> {
    let kind = args.get_item(0)?;
    let value = if kind.is_instance_of::<pyo3::exceptions::PyBaseException>() {
        kind
    } else if args.len() > 1 && !args.get_item(1)?.is_none() {
        let value = args.get_item(1)?;
        if value.is_instance(&kind)? {
            value
        } else {
            kind.call1((value,))?
        }
    } else {
        kind.call0()?
    };
    let value = if args.len() > 2 && !args.get_item(2)?.is_none() {
        value.call_method1("with_traceback", (args.get_item(2)?,))?
    } else {
        value
    };
    let _ = py;
    Ok(PyErr::from_value(value))
}
fn advance_iterator<'py>(
    _py: Python<'py>,
    iterator: &Bound<'py, PyAny>,
    value: Option<&Bound<'py, PyAny>>,
) -> PyResult<Bound<'py, PyAny>> {
    match value {
        Some(value) if !value.is_none() => iterator.call_method1("send", (value,)),
        _ => iterator.call_method0("__next__"),
    }
}
fn await_iterator<'py>(
    py: Python<'py>,
    awaitable: &Bound<'py, PyAny>,
) -> PyResult<Bound<'py, PyAny>> {
    match awaitable.getattr("__await__") {
        Ok(method) => {
            let iterator = method.call0()?;
            if !iterator.hasattr("__next__")? {
                return Err(PyTypeError::new_err("__await__() returned a non-iterator"));
            }
            Ok(iterator)
        }
        Err(error) if error.is_instance_of::<PyAttributeError>(py) => {
            if py
                .import("inspect")?
                .call_method1("isawaitable", (awaitable,))?
                .is_truthy()?
            {
                Ok(awaitable.clone())
            } else {
                Err(PyTypeError::new_err(format!(
                    "object {} can't be used in 'await' expression",
                    awaitable.get_type().name()?
                )))
            }
        }
        Err(error) => Err(error),
    }
}
impl HostCoroutine {
    fn advance(
        &mut self,
        py: Python<'_>,
        input: Option<&Bound<'_, PyAny>>,
        thrown: Option<&Bound<'_, PyTuple>>,
    ) -> Output {
        if self
            .future
            .get_mut()
            .unwrap_or_else(|e| e.into_inner())
            .is_none()
        {
            return Err(PyRuntimeError::new_err(
                "cannot reuse already awaited coroutine",
            ));
        }
        if !self.started && input.is_some_and(|value| !value.is_none()) {
            return Err(PyTypeError::new_err(
                "can't send non-None value to a just-started coroutine",
            ));
        }
        self.started = true;
        if let Some(awaiting) = self.awaiting.take() {
            let result = if let Some(args) = thrown {
                match awaiting.iterator.bind(py).getattr("throw") {
                    Ok(throw) => throw.call(args, None),
                    Err(error) if error.is_instance_of::<PyAttributeError>(py) => {
                        Err(thrown_error(py, args)?)
                    }
                    Err(error) => Err(error),
                }
            } else {
                advance_iterator(py, awaiting.iterator.bind(py), input)
            };
            match result {
                Ok(value) => {
                    self.awaiting = Some(awaiting);
                    return Ok(value.unbind());
                }
                Err(error) => {
                    let result = if error.is_instance_of::<PyStopIteration>(py) {
                        error.value(py).getattr("value").map(Bound::unbind)
                    } else {
                        Err(error)
                    };
                    *awaiting.result.lock().unwrap_or_else(|e| e.into_inner()) = Some(result);
                }
            }
        } else if let Some(args) = thrown {
            *self.future.get_mut().unwrap_or_else(|e| e.into_inner()) = None;
            return Err(thrown_error(py, args)?);
        }
        loop {
            let previous = REQUEST.with(|slot| slot.borrow_mut().take());
            let waker = Waker::noop();
            let polled = self
                .future
                .get_mut()
                .unwrap_or_else(|e| e.into_inner())
                .as_mut()
                .ok_or_else(|| PyRuntimeError::new_err("coroutine closed"))?
                .as_mut()
                .poll(&mut Context::from_waker(waker));
            let requested =
                REQUEST.with(|slot| std::mem::replace(&mut *slot.borrow_mut(), previous));
            match polled {
                Poll::Ready(result) => {
                    *self.future.get_mut().unwrap_or_else(|e| e.into_inner()) = None;
                    return match result {
                        Ok(value) => Err(PyStopIteration::new_err((value,))),
                        Err(error) => Err(error),
                    };
                }
                Poll::Pending => {
                    let request = requested.ok_or_else(|| {
                        PyRuntimeError::new_err(
                            "native host coroutine waited outside its host await bridge",
                        )
                    })?;
                    let iterator = match await_iterator(py, request.awaitable.bind(py)) {
                        Ok(value) => value,
                        Err(error) => {
                            *request.result.lock().unwrap_or_else(|e| e.into_inner()) =
                                Some(Err(error));
                            continue;
                        }
                    };
                    match advance_iterator(py, &iterator, None) {
                        Ok(value) => {
                            self.awaiting = Some(Awaiting {
                                iterator: iterator.unbind(),
                                result: request.result,
                            });
                            return Ok(value.unbind());
                        }
                        Err(error) => {
                            let result = if error.is_instance_of::<PyStopIteration>(py) {
                                error.value(py).getattr("value").map(Bound::unbind)
                            } else {
                                Err(error)
                            };
                            *request.result.lock().unwrap_or_else(|e| e.into_inner()) =
                                Some(result);
                        }
                    }
                }
            }
        }
    }
}
#[pymethods]
impl HostCoroutine {
    fn __await__(slf: PyRef<'_, Self>) -> PyRef<'_, Self> {
        slf
    }
    fn __iter__(slf: PyRef<'_, Self>) -> PyRef<'_, Self> {
        slf
    }
    fn __next__(&mut self, py: Python<'_>) -> Output {
        self.advance(py, None, None)
    }
    fn send(&mut self, py: Python<'_>, value: &Bound<'_, PyAny>) -> Output {
        self.advance(py, Some(value), None)
    }
    #[pyo3(signature = (*args))]
    fn throw(&mut self, py: Python<'_>, args: &Bound<'_, PyTuple>) -> Output {
        self.advance(py, None, Some(args))
    }
    fn close(&mut self, py: Python<'_>) -> PyResult<()> {
        if !self.started
            || self
                .future
                .get_mut()
                .unwrap_or_else(|e| e.into_inner())
                .is_none()
        {
            *self.future.get_mut().unwrap_or_else(|e| e.into_inner()) = None;
            return Ok(());
        }
        // Like Python's yield-from close protocol, close the delegated iterator,
        // then inject GeneratorExit into the suspended Rust continuation. Dropping
        // the future directly would skip async-context-manager cleanup.
        if let Some(awaiting) = self.awaiting.take() {
            let error = match awaiting.iterator.bind(py).getattr("close") {
                Ok(close) => close
                    .call0()
                    .err()
                    .unwrap_or_else(|| PyGeneratorExit::new_err(())),
                Err(error) if error.is_instance_of::<PyAttributeError>(py) => {
                    PyGeneratorExit::new_err(())
                }
                Err(error) => error,
            };
            *awaiting.result.lock().unwrap_or_else(|e| e.into_inner()) = Some(Err(error));
        }
        match self.advance(py, None, None) {
            Err(error)
                if error.is_instance_of::<PyGeneratorExit>(py)
                    || error.is_instance_of::<PyStopIteration>(py) =>
            {
                Ok(())
            }
            Err(error) => Err(error),
            Ok(_) => Err(PyRuntimeError::new_err("coroutine ignored GeneratorExit")),
        }
    }
}
pub fn register(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_class::<HostCoroutine>()?;
    module
        .py()
        .import("collections.abc")?
        .getattr("Coroutine")?
        .call_method1("register", (module.getattr("HostCoroutine")?,))?;
    Ok(())
}

/// A foreign callback marker for SDKs using asyncio's coroutine-function check.
/// Invocation stays native; the wrapped callable supplies the host await protocol.
#[pyclass(module = "agentchat._native")]
struct AsyncCallback {
    callable: Option<Py<PyAny>>,
}
#[pymethods]
impl AsyncCallback {
    fn __traverse__(&self, visit: pyo3::PyVisit<'_>) -> Result<(), pyo3::PyTraverseError> {
        visit.call(&self.callable)
    }
    fn __clear__(&mut self) {
        self.callable = None;
    }
    // Python 3.11 inspect recognizes compiled function-like callables through
    // their code flags. This object is introspection metadata only: __call__
    // always invokes the native callable and never executes the borrowed code.
    #[getter]
    fn __code__(&self, py: Python<'_>) -> Output {
        Ok(py
            .import("asyncio")?
            .getattr("sleep")?
            .getattr("__code__")?
            .unbind())
    }
    #[getter]
    fn __defaults__(&self, py: Python<'_>) -> Py<PyAny> {
        py.None()
    }
    #[getter]
    fn __kwdefaults__(&self, py: Python<'_>) -> Py<PyAny> {
        py.None()
    }
    #[getter]
    fn __annotations__(&self, py: Python<'_>) -> Py<PyAny> {
        py.None()
    }
    #[getter]
    fn _is_coroutine(&self, py: Python<'_>) -> Output {
        Ok(py
            .import("asyncio.coroutines")?
            .getattr("_is_coroutine")?
            .unbind())
    }
    #[getter]
    fn __name__(&self) -> &'static str {
        "agentchat_callback"
    }
    #[pyo3(signature = (*args, **kwargs))]
    fn __call__(
        &self,
        py: Python<'_>,
        args: &Bound<'_, PyTuple>,
        kwargs: Option<&Bound<'_, pyo3::types::PyDict>>,
    ) -> Output {
        Ok(self
            .callable
            .as_ref()
            .ok_or_else(|| PyRuntimeError::new_err("callback has been cleared"))?
            .bind(py)
            .call(args, kwargs)?
            .unbind())
    }
}
pub fn callback(py: Python<'_>, callable: Py<PyAny>) -> Output {
    Ok(Py::new(
        py,
        AsyncCallback {
            callable: Some(callable),
        },
    )?
    .into_any())
}

/// Expose ordinary Python construction semantics without moving initialization
/// into `__new__`. Subclasses may accept their own arguments in `__init__` and
/// forward different arguments to the native initializer through `super()`.
pub fn register_initialized_class<T: pyo3::PyClass>(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_class::<T>()?;
    let class = module.py().get_type::<T>();
    class.setattr("__init__", class.getattr("initialize")?)
}
