import assert from "node:assert/strict";
import { test } from "node:test";

import {
  buildGraphPageUrl,
  fetchGraphPage,
  fetchFullGraphFile,
  loadGraphFileContent,
  mergeGraphPage,
  readGraphPageOptions,
} from "../src/lib/localGraphApi.js";

test("Explore reads the requested initial graph offset and page size", () => {
  const options = readGraphPageOptions(new URLSearchParams("offset=12&page_size=7"));
  assert.deepEqual(options, { offset: 12, pageSize: 7 });
  assert.deepEqual(readGraphPageOptions(new URLSearchParams()), { offset: 0 });
});

test("Explore ignores invalid initial graph page values", () => {
  assert.deepEqual(
    readGraphPageOptions(new URLSearchParams("offset=-1&page_size=nope")),
    { offset: 0 },
  );
});

test("selecting a preview-marked file loads full source on demand", async () => {
  const requestedPaths = [];
  const content = await loadGraphFileContent(
    "/work/repo/large.py",
    "preview only",
    true,
    async (path) => {
      requestedPaths.push(path);
      return "complete source";
    },
  );

  assert.deepEqual(requestedPaths, ["/work/repo/large.py"]);
  assert.equal(content, "complete source");

  const fileRequests = [];
  const filePages = [
    { content: "first part", next_offset: 10 },
    { content: "second part", next_offset: null },
  ];
  const fullContent = await fetchFullGraphFile(
    "http://localhost:8000",
    "/work/repo/large.py",
    async (requestUrl) => {
      fileRequests.push(new URL(requestUrl));
      return { ok: true, json: async () => filePages.shift() };
    },
  );
  assert.equal(fullContent, "first partsecond part");
  assert.deepEqual(fileRequests.map((url) => url.searchParams.get("offset")), ["0", "10"]);
  assert.ok(fileRequests.every((url) => url.searchParams.get("page_size")));
});

test("selecting a file without a graph preview loads source on demand", async () => {
  const requestedPaths = [];
  const content = await loadGraphFileContent(
    "/work/repo/file-outside-preview-cap.py",
    null,
    false,
    async (path) => {
      requestedPaths.push(path);
      return "complete source outside the preview window";
    },
  );

  assert.deepEqual(requestedPaths, ["/work/repo/file-outside-preview-cap.py"]);
  assert.equal(content, "complete source outside the preview window");
});

test("continuation pages merge without duplicate graph records and retain the next cursor", async () => {
  const current = {
    nodes: [{ id: "node-1" }],
    links: [{ id: "edge-1", source: "node-1", target: "node-2" }],
    files: ["a.py"],
    fileContents: { "a.py": "source" },
    fileContentsTruncated: {},
    next_offset: 1,
    page_size: 1,
  };
  const next = {
    nodes: [{ id: "node-1" }, { id: "node-2" }],
    links: [
      { id: "edge-1", source: "node-1", target: "node-2" },
      { id: "edge-2", source: "node-2", target: "node-3" },
    ],
    files: ["a.py", "b.py"],
    fileContents: { "b.py": "more source" },
    fileContentsTruncated: { "b.py": true },
    next_offset: 2,
    page_size: 1,
    truncated: true,
    truncation_reasons: ["row_limit"],
  };

  const merged = mergeGraphPage(current, next);

  assert.deepEqual(merged.nodes.map((node) => node.id), ["node-1", "node-2"]);
  assert.deepEqual(merged.links.map((link) => link.id), ["edge-1", "edge-2"]);
  assert.deepEqual(merged.files, ["a.py", "b.py"]);
  assert.equal(merged.fileContents["b.py"], "more source");
  assert.equal(merged.next_offset, 2);

  const url = buildGraphPageUrl({
    baseUrl: "http://localhost:8000",
    repoPath: "/work/repo",
    cypherQuery: "MATCH (f:Function) RETURN f",
    offset: merged.next_offset,
    pageSize: merged.page_size,
  });
  assert.equal(url.searchParams.get("offset"), "2");
  assert.equal(url.searchParams.get("page_size"), "1");
  assert.equal(url.searchParams.get("repo_path"), "/work/repo");
  assert.equal(url.searchParams.get("cypher_query"), "MATCH (f:Function) RETURN f");

  const requests = [];
  const pages = [
    { retry_required: true, retry_offset: 7, retry_page_size: 2 },
    { offset: 7, page_size: 2, next_offset: 9 },
  ];
  const fetched = await fetchGraphPage({
    baseUrl: "http://localhost:8000",
    offset: 7,
    pageSize: 4,
    fetchImpl: async (requestUrl) => {
      requests.push(new URL(requestUrl));
      return { ok: true, json: async () => pages.shift() };
    },
  });
  assert.equal(fetched.next_offset, 9);
  assert.equal(requests.length, 2);
  assert.equal(requests[0].searchParams.get("offset"), "7");
  assert.equal(requests[0].searchParams.get("page_size"), "4");
  assert.equal(requests[1].searchParams.get("offset"), "7");
  assert.equal(requests[1].searchParams.get("page_size"), "2");
});

test("global graph scope is forwarded on initial and smaller retry requests", async () => {
  const requests = [];
  const pages = [
    { retry_required: true, retry_offset: 0, retry_page_size: 2 },
    { offset: 0, page_size: 2, next_offset: 2 },
  ];
  const page = await fetchGraphPage({
    baseUrl: "http://localhost:8000",
    cypherQuery: "MATCH (f:Function) RETURN f",
    globalScope: true,
    pageSize: 4,
    fetchImpl: async (requestUrl) => {
      requests.push(new URL(requestUrl));
      return { ok: true, json: async () => pages.shift() };
    },
  });

  assert.equal(page.next_offset, 2);
  assert.equal(requests.length, 2);
  assert.ok(requests.every((url) => url.searchParams.get("global_scope") === "true"));
  assert.deepEqual(
    requests.map((url) => url.searchParams.get("page_size")),
    ["4", "2"],
  );
});
