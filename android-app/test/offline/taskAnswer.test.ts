import { test } from "node:test";
import assert from "node:assert/strict";
import { EvoAgentClient } from "../../src/api.ts";

test("answer sends the question id and answer to the task endpoint", async () => {
  const original = globalThis.fetch;
  const requests: { url: string; init?: RequestInit }[] = [];
  globalThis.fetch = async (url, init) => {
    requests.push({ url: String(url), init });
    return new Response(JSON.stringify({ task_id: "task_1", accepted: true, run_id: "run_2" }), {
      status: 202, headers: { "Content-Type": "application/json" },
    });
  };
  try {
    const client = new EvoAgentClient("http://localhost:8300");
    const result = await client.answerTask("task_1", "question_1", "保存到笔记");
    assert.equal(result.accepted, true);
    assert.equal(requests[0].url, "http://localhost:8300/tasks/task_1/answer");
    assert.equal(requests[0].init?.method, "POST");
    assert.deepEqual(JSON.parse(String(requests[0].init?.body)), {
      request_id: "question_1", answer: "保存到笔记",
    });
  } finally {
    globalThis.fetch = original;
  }
});

test("approval requests send the admin credential only on the explicit admin call", async () => {
  const original = globalThis.fetch;
  const requests: { url: string; init?: RequestInit }[] = [];
  globalThis.fetch = async (url, init) => {
    requests.push({ url: String(url), init });
    return new Response(JSON.stringify({ accepted: true }), {
      status: 200, headers: { "Content-Type": "application/json" },
    });
  };
  try {
    const client = new EvoAgentClient("http://localhost:8300");
    await client.getApproval("apr_1", "admin-token");
    await client.decideApproval("apr_1", false, "admin-token");
    await client.getTask("task_1");
    assert.equal(requests[0].url, "http://localhost:8300/approvals/apr_1");
    assert.equal(new Headers(requests[0].init?.headers).get("Authorization"), "Bearer admin-token");
    assert.equal(requests[1].url, "http://localhost:8300/approvals/apr_1/decision");
    assert.deepEqual(JSON.parse(String(requests[1].init?.body)), { approved: false });
    assert.equal(new Headers(requests[1].init?.headers).get("Authorization"), "Bearer admin-token");
    assert.equal(new Headers(requests[2].init?.headers).get("Authorization"), null);
  } finally {
    globalThis.fetch = original;
  }
});
