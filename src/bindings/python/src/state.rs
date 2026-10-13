use crate::{host::coroutine, NativeMemoryStore};
use pyo3::{
    exceptions::PyValueError,
    prelude::*,
    types::{PyDict, PySlice, PyTuple},
    PyTraverseError, PyVisit,
};

pub fn backend(backend: Option<&str>) -> PyResult<&'static str> {
    if backend.is_some_and(|value| value != "rust") {
        return Err(PyValueError::new_err(
            "backend must be 'rust'; the Python implementation has been removed",
        ));
    }
    Ok("rust")
}
#[pyclass(dict, subclass, weakref, module = "agentchat._native")]
pub struct MemoryState {
    inner: Option<Box<MemoryStateState>>,
}
pub struct MemoryStateState {
    store: Py<NativeMemoryStore>,
    locks: Py<PyDict>,
}
impl std::ops::Deref for MemoryState {
    type Target = MemoryStateState;
    fn deref(&self) -> &Self::Target {
        self.inner
            .as_deref()
            .expect("native object has been cleared")
    }
}
impl std::ops::DerefMut for MemoryState {
    fn deref_mut(&mut self) -> &mut Self::Target {
        self.inner
            .as_deref_mut()
            .expect("native object has been cleared")
    }
}

#[pymethods]
impl MemoryState {
    #[new]
    #[pyo3(signature=(*_args, **_kwargs))]
    fn new(
        _args: &Bound<'_, pyo3::types::PyTuple>,
        _kwargs: Option<&Bound<'_, pyo3::types::PyDict>>,
    ) -> Self {
        Self { inner: None }
    }
    #[pyo3(signature=(*,backend=None))]
    fn initialize(&mut self, py: Python<'_>, backend: Option<&str>) -> PyResult<()> {
        *self = Self::build(py, backend)?;
        Ok(())
    }
    fn claim(slf: Py<Self>, py: Python<'_>, message_id: String) -> PyResult<Py<PyAny>> {
        coroutine(py, async move {
            Python::attach(|py| {
                Ok(slf
                    .borrow(py)
                    .store
                    .borrow_mut(py)
                    .claim(&message_id)
                    .into_pyobject(py)?
                    .to_owned()
                    .unbind()
                    .into_any())
            })
        })
    }
    fn lock(&self, py: Python<'_>, conversation_id: &str) -> PyResult<Py<PyAny>> {
        if let Some(lock) = self.locks.bind(py).get_item(conversation_id)? {
            return Ok(lock.unbind());
        }
        let lock = py.import("asyncio")?.getattr("Lock")?.call0()?;
        self.locks.bind(py).set_item(conversation_id, &lock)?;
        Ok(lock.unbind())
    }
    fn append(slf: Py<Self>, py: Python<'_>, message: Py<PyAny>) -> PyResult<Py<PyAny>> {
        coroutine(py, async move {
            Python::attach(|py| {
                let conversation: String =
                    message.bind(py).getattr("conversation_id")?.extract()?;
                slf.borrow(py)
                    .store
                    .borrow_mut(py)
                    .append(py, &conversation, message)?;
                Ok(py.None())
            })
        })
    }
    #[pyo3(signature=(conversation_id,*,limit=None))]
    fn history(
        slf: Py<Self>,
        py: Python<'_>,
        conversation_id: String,
        limit: Option<Py<PyAny>>,
    ) -> PyResult<Py<PyAny>> {
        coroutine(py, async move {
            Python::attach(|py| {
                let view = slf.borrow(py).store.borrow(py).messages(py)?;
                let messages = view
                    .bind(py)
                    .get_item(&conversation_id)?
                    .unwrap_or_else(|| PyTuple::empty(py).into_any());
                match limit {
                    None => Ok(messages.unbind()),
                    Some(limit) => {
                        let neg = limit.bind(py).call_method0("__neg__")?;
                        let slice = py.import("builtins")?.getattr("slice")?.call1((
                            neg,
                            py.None(),
                            py.None(),
                        ))?;
                        Ok(messages.get_item(slice.cast::<PySlice>()?)?.unbind())
                    }
                }
            })
        })
    }
    #[getter]
    fn messages(&self, py: Python<'_>) -> PyResult<Py<PyAny>> {
        Ok(py
            .import("types")?
            .getattr("MappingProxyType")?
            .call1((self.store.borrow(py).messages(py)?,))?
            .unbind())
    }
    fn __traverse__(&self, visit: PyVisit<'_>) -> Result<(), PyTraverseError> {
        let Some(state) = &self.inner else {
            return Ok(());
        };

        visit.call(&state.store)?;
        visit.call(&state.locks)
    }

    fn __clear__(&mut self) {
        self.inner = None;
    }
}
pub fn register(module: &Bound<'_, PyModule>) -> PyResult<()> {
    crate::host::register_initialized_class::<MemoryState>(module)
}

impl MemoryState {
    #[allow(clippy::too_many_arguments)]
    fn build(py: Python<'_>, backend: Option<&str>) -> PyResult<Self> {
        self::backend(backend)?;
        Ok(Self {
            inner: Some(Box::new(MemoryStateState {
                store: Py::new(py, NativeMemoryStore::new(py))?,
                locks: PyDict::new(py).unbind(),
            })),
        })
    }
}
