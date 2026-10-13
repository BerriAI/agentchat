use crate::{
    host::{await_py, coroutine},
    runtime::{call, copy, exit_scope, none},
};
use pyo3::{
    exceptions::{PyException, PyRuntimeError, PyValueError},
    prelude::*,
    types::{PyDict, PyTuple},
    PyTraverseError, PyVisit,
};
type Object = Py<PyAny>;
#[pyclass(dict, subclass, weakref, module = "agentchat._native")]
pub struct WorkingStatus {
    inner: Option<Box<WorkingStatusState>>,
}
pub struct WorkingStatusState {
    setter: Object,
    status: String,
    interval: f64,
    lock: Object,
    task: Option<Object>,
}
impl std::ops::Deref for WorkingStatus {
    type Target = WorkingStatusState;
    fn deref(&self) -> &Self::Target {
        self.inner
            .as_deref()
            .expect("native object has been cleared")
    }
}
impl std::ops::DerefMut for WorkingStatus {
    fn deref_mut(&mut self) -> &mut Self::Target {
        self.inner
            .as_deref_mut()
            .expect("native object has been cleared")
    }
}

async fn send(setter: Object, status: String) -> PyResult<Object> {
    let timeout = Python::attach(|py| {
        py.import("asyncio")?
            .call_method1("timeout", (3,))
            .map(Bound::unbind)
    })?;
    call(copy(&timeout), "__aenter__", vec![], vec![]).await?;
    let result = async {
        let pending = Python::attach(|py| setter.bind(py).call1((status,)).map(Bound::unbind))?;
        await_py(pending).await
    }
    .await;
    let exit = exit_scope(timeout, result.as_ref().err()).await;
    let result = match exit {
        Err(error) => Err(error),
        Ok(true) => Ok(none()),
        Ok(false) => result,
    };
    match result {
        Ok(value) => Ok(value),
        Err(error) => Python::attach(|py| {
            if !error.is_instance_of::<PyException>(py) {
                return Err(error);
            }
            py.import("logging")?
                .call_method1("getLogger", ("agentchat.activity",))?
                .call_method1(
                    "warning",
                    (
                        "Working status unavailable: %s",
                        error.get_type(py).getattr("__name__")?,
                    ),
                )?;
            Ok(false.into_pyobject(py)?.to_owned().unbind().into_any())
        }),
    }
}
async fn update(this: Py<WorkingStatus>, status: String) -> PyResult<Object> {
    let lock = Python::attach(|py| copy(&this.borrow(py).lock));
    call(copy(&lock), "__aenter__", vec![], vec![]).await?;
    let result = async {
        let setter = Python::attach(|py| {
            let mut this = this.borrow_mut(py);
            this.status = status.clone();
            copy(&this.setter)
        });
        send(setter, status).await
    }
    .await;
    exit_scope(lock, result.as_ref().err()).await?;
    result
}
#[pymethods]
impl WorkingStatus {
    #[new]
    #[pyo3(signature=(*_args, **_kwargs))]
    fn new(
        _args: &Bound<'_, pyo3::types::PyTuple>,
        _kwargs: Option<&Bound<'_, pyo3::types::PyDict>>,
    ) -> Self {
        Self { inner: None }
    }
    #[pyo3(signature=(setter,status,*,refresh_interval=60.0))]
    fn initialize(
        &mut self,
        py: Python<'_>,
        setter: Object,
        status: String,
        refresh_interval: f64,
    ) -> PyResult<()> {
        *self = Self::build(py, setter, status, refresh_interval)?;
        Ok(())
    }
    fn update(slf: Py<Self>, py: Python<'_>, status: String) -> PyResult<Object> {
        coroutine(py, update(slf, status))
    }
    fn __aenter__(slf: Py<Self>, py: Python<'_>) -> PyResult<Object> {
        coroutine(py, async move {
            let status = Python::attach(|py| -> PyResult<_> {
                let this = slf.borrow(py);
                if this.task.is_some() {
                    return Err(PyRuntimeError::new_err(
                        "A working status context cannot be entered twice",
                    ));
                }
                Ok(this.status.clone())
            })?;
            let pending = Python::attach(|py| {
                slf.bind(py)
                    .call_method1("update", (status,))
                    .map(Bound::unbind)
            })?;
            await_py(pending).await?;
            Python::attach(|py| {
                let this = slf.clone_ref(py);
                let refresh = coroutine(py, async move {
                    loop {
                        let (interval, lock) = Python::attach(|py| {
                            let this = this.borrow(py);
                            (this.interval, copy(&this.lock))
                        });
                        let sleeping = Python::attach(|py| {
                            py.import("asyncio")?
                                .call_method1("sleep", (interval,))
                                .map(Bound::unbind)
                        })?;
                        await_py(sleeping).await?;
                        call(copy(&lock), "__aenter__", vec![], vec![]).await?;
                        let (setter, status) = Python::attach(|py| {
                            let this = this.borrow(py);
                            (copy(&this.setter), this.status.clone())
                        });
                        let result = send(setter, status).await;
                        exit_scope(lock, result.as_ref().err()).await?;
                        result?;
                    }
                })?;
                let task = py
                    .import("asyncio")?
                    .call_method1("create_task", (refresh,))?
                    .unbind();
                slf.borrow_mut(py).task = Some(task);
                Ok(slf.into_any())
            })
        })
    }
    fn __aexit__(
        slf: Py<Self>,
        py: Python<'_>,
        _exc_type: Object,
        _exc: Object,
        _traceback: Object,
    ) -> PyResult<Object> {
        coroutine(py, async move {
            let waiting = Python::attach(|py| -> PyResult<_> {
                if let Some(task) = slf.borrow(py).task.as_ref() {
                    task.bind(py).call_method0("cancel")?;
                    let kw = PyDict::new(py);
                    kw.set_item("return_exceptions", true)?;
                    Ok(Some(
                        py.import("asyncio")?
                            .call_method("gather", PyTuple::new(py, [task.bind(py)])?, Some(&kw))?
                            .unbind(),
                    ))
                } else {
                    Ok(None)
                }
            })?;
            if let Some(waiting) = waiting {
                await_py(waiting).await?;
            }
            let pending = Python::attach(|py| {
                slf.bind(py)
                    .call_method1("update", ("",))
                    .map(Bound::unbind)
            })?;
            await_py(pending).await?;
            Ok(none())
        })
    }
    fn __traverse__(&self, visit: PyVisit<'_>) -> Result<(), PyTraverseError> {
        let Some(state) = &self.inner else {
            return Ok(());
        };

        visit.call(&state.setter)?;
        visit.call(&state.lock)?;
        visit.call(&state.task)
    }

    fn __clear__(&mut self) {
        self.inner = None;
    }
}
pub fn register(module: &Bound<'_, PyModule>) -> PyResult<()> {
    crate::host::register_initialized_class::<WorkingStatus>(module)
}

impl WorkingStatus {
    #[allow(clippy::too_many_arguments)]
    fn build(
        py: Python<'_>,
        setter: Object,
        status: String,
        refresh_interval: f64,
    ) -> PyResult<Self> {
        if !(refresh_interval > 0.0 && refresh_interval <= 90.0) {
            return Err(PyValueError::new_err(
                "Refresh interval must be greater than 0 and at most 90 seconds",
            ));
        }
        Ok(Self {
            inner: Some(Box::new(WorkingStatusState {
                setter,
                status,
                interval: refresh_interval,
                lock: py.import("asyncio")?.getattr("Lock")?.call0()?.unbind(),
                task: None,
            })),
        })
    }
}
