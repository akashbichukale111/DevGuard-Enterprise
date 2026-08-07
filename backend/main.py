import os
from contextlib import asynccontextmanager
from fastapi import FastAPI
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.logging import LoggingInstrumentor
from backend.core import telemetry, cache
from backend.api.router import router
from backend.api.god_mode_simulators import god_mode_router
from fastapi.middleware.cors import CORSMiddleware

@asynccontextmanager
async def lifespan(app: FastAPI):
    telemetry.init_telemetry()
    # GOD-TIER ADDITION: auto-inject trace_id/span_id into every stdlib log
    # record's formatted output, so raw console logs are correlated to
    # traces even before they're viewed inside SigNoz's log explorer.
    LoggingInstrumentor().instrument(set_logging_format=True)
    await cache.init_cache()
    yield
    await cache.close_cache()
    telemetry.shutdown_telemetry()

app = FastAPI(title="DevGuard AI", lifespan=lifespan)

# CORS origins are configurable so a deployment can be narrowed without a code
# change. The default stays "*" deliberately: this backend is also run locally,
# from containers, and from a static export served on arbitrary ports, and
# silently breaking every one of those to harden a demo would be the wrong
# trade. `allow_credentials` is False, so "*" here does not expose cookies or
# authenticated sessions — there are none.
#
# Production should set DEVGUARD_ALLOWED_ORIGINS to the real frontend origin;
# render.yaml documents it. Comma-separated, whitespace tolerated.
_origins_raw = os.environ.get("DEVGUARD_ALLOWED_ORIGINS", "*").strip()
ALLOWED_ORIGINS = (
    ["*"] if _origins_raw == "*" else [o.strip() for o in _origins_raw.split(",") if o.strip()]
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

FastAPIInstrumentor.instrument_app(app)
app.include_router(router)
app.include_router(god_mode_router)