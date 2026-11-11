import asyncio
import json

import httpx

from elice_knowledge.judge_efficiency import Collector, hooks, response_usage


def test_usage_for_json_stream_and_missing():
    assert response_usage(
        json.dumps(
            {
                "usage": {
                    "prompt_tokens": 10,
                    "completion_tokens": 4,
                    "total_tokens": 14,
                    "prompt_tokens_details": {"cached_tokens": 2},
                }
            }
        ).encode()
    ) == {"input_tokens": 10, "cached_input_tokens": 2, "output_tokens": 4, "total_tokens": 14}
    assert (
        response_usage(
            b'data: {"choices":[]}\n\ndata: {"usage":{"prompt_tokens":3}}\n\ndata: [DONE]\n'
        )["input_tokens"]
        == 3
    )
    assert response_usage(b"data: [DONE]\n")["input_tokens"] is None


def test_response_hooks_keep_stream_readable_and_do_not_store_content():
    async def run():
        calls = []
        body = (
            b'data: {"choices":[]}\n\n'
            b'data: {"usage":{"prompt_tokens":7,"completion_tokens":2}}\n\ndata: [DONE]\n'
        )

        class StreamingBody(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield body[:20]
                yield body[20:]

        async def handler(request):
            return httpx.Response(
                200, headers={"content-type": "text/event-stream"}, stream=StreamingBody()
            )

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler), event_hooks=hooks(calls)
        ) as client:
            async with client.stream("POST", "https://test.invalid/chat/completions") as response:
                assert await response.aread() == body
        assert calls[0]["input_tokens"] == 7 and calls[0]["output_tokens"] == 2
        assert calls[0]["duration_ms"] >= 0 and "choices" not in str(calls)

    asyncio.run(run())


def test_collector_connects_dimension_and_record():
    records = [{"id": "q1", "inputs": {"question": "Q"}, "expectations": {"answer": "A"}}]
    cases = {"q1": {"result": {"answer": "X"}}}
    collector = Collector(records, cases)
    import time

    collector.record(
        records[0]["inputs"],
        cases["q1"]["result"],
        records[0]["expectations"],
        "correctness",
        time.monotonic(),
        [{"input_tokens": 7}],
        "scored",
    )
    artifact = collector.artifact(run_id="grading", elapsed_ms=20)
    assert (artifact["rows"][0]["record_id"], artifact["rows"][0]["dimension"]) == (
        "q1",
        "correctness",
    )
    assert artifact["rows"][0]["calls"][0]["input_tokens"] == 7


def test_openai_sdk_can_consume_stream_after_observation():
    from openai import AsyncOpenAI

    async def run():
        calls = []
        chunks = [
            {
                "id": "x",
                "object": "chat.completion.chunk",
                "created": 1,
                "model": "test",
                "choices": [
                    {
                        "index": 0,
                        "delta": {"role": "assistant", "content": "hello"},
                        "finish_reason": None,
                    }
                ],
            },
            {
                "id": "x",
                "object": "chat.completion.chunk",
                "created": 1,
                "model": "test",
                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 7, "completion_tokens": 1, "total_tokens": 8},
            },
        ]
        body = b"".join(b"data: " + json.dumps(chunk).encode() + b"\n\n" for chunk in chunks)
        body += b"data: [DONE]\n\n"

        class StreamingBody(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield body[:35]
                yield body[35:]

        async def handler(request):
            return httpx.Response(
                200, headers={"content-type": "text/event-stream"}, stream=StreamingBody()
            )

        http = httpx.AsyncClient(transport=httpx.MockTransport(handler), event_hooks=hooks(calls))
        async with AsyncOpenAI(
            base_url="https://test.invalid/v1", api_key="test", http_client=http
        ) as client:
            stream = await client.chat.completions.create(
                model="test", messages=[{"role": "user", "content": "x"}], stream=True
            )
            result = [chunk async for chunk in stream]
        assert result[0].choices[0].delta.content == "hello"
        assert calls[0]["total_tokens"] == 8

    asyncio.run(run())
