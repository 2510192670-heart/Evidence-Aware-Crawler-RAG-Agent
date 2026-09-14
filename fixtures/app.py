"""FastAPI app aggregating every loopback fixture structure.

Run it with::

    python -m fixtures

or::

    python -m uvicorn fixtures.app:app --host 127.0.0.1 --port 8100

Benchmark tasks in ``benchmarks/tasks.json`` point at this app.
"""

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse

from .pages import PAGE_SPECS, render_page
from .scenarios import ROUTERS

DEFAULT_PORT = 8100

app = FastAPI(title='Web Data Agent loopback fixtures', version='1.0.0')
for _router in ROUTERS:
    app.include_router(_router)


@app.get('/', include_in_schema=False)
def index():
    return {'pages': sorted(PAGE_SPECS), 'default_port': DEFAULT_PORT}


@app.get('/pages/{name}', response_class=HTMLResponse, include_in_schema=False)
def page(name: str):
    if name not in PAGE_SPECS:
        raise HTTPException(status_code=404, detail='unknown_fixture_page')
    return render_page(name)
