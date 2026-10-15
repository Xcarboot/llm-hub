import asyncio
import logging
from collections.abc import Awaitable, Callable, Mapping
from typing import cast

from starlette.background import BackgroundTask
from starlette.responses import ContentStream, StreamingResponse
from starlette.types import Send

logger = logging.getLogger(__name__)


class ClosingStreamingResponse(StreamingResponse):
    """Close an interrupted response body before propagating the send failure."""

    def __init__(
        self,
        content: ContentStream,
        status_code: int = 200,
        headers: Mapping[str, str] | None = None,
        media_type: str | None = None,
        background: BackgroundTask | None = None,
    ) -> None:
        super().__init__(content, status_code, headers, media_type, background)
        self._body_close_task: asyncio.Task[None] | None = None

    async def _close_body_iterator(self) -> None:
        close = getattr(self.body_iterator, "aclose", None)
        if close is None:
            return
        if self._body_close_task is None:
            close_callable = cast(Callable[[], Awaitable[None]], close)

            async def close_body() -> None:
                await close_callable()

            self._body_close_task = asyncio.create_task(close_body())

        close_task = self._body_close_task
        while not close_task.done():
            try:
                await asyncio.shield(close_task)
            except asyncio.CancelledError:
                # A request task may be cancelled repeatedly while cleanup owns
                # settlement and lease release. Keep waiting for that cleanup.
                continue
            except BaseException:
                break
        try:
            close_task.result()
        except BaseException as exc:
            logger.error(
                "failed to close interrupted response body",
                exc_info=(type(exc), exc, exc.__traceback__),
            )

    async def stream_response(self, send: Send) -> None:
        completed = False
        failure: BaseException | None = None
        try:
            await super().stream_response(send)
            completed = True
        except BaseException as exc:
            failure = exc
        finally:
            if not completed:
                await self._close_body_iterator()

        if failure is not None:
            raise failure.with_traceback(failure.__traceback__)
