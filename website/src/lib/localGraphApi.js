export function readGraphPageOptions(searchParams) {
  const rawOffset = searchParams.get("offset");
  const parsedOffset = rawOffset === null ? 0 : Number(rawOffset);
  const offset = Number.isSafeInteger(parsedOffset)
    && parsedOffset >= 0
    && parsedOffset < 100_000
    ? parsedOffset
    : 0;

  const rawPageSize = searchParams.get("page_size");
  if (rawPageSize === null) return { offset };
  const pageSize = Number(rawPageSize);
  if (!Number.isSafeInteger(pageSize) || pageSize < 1) return { offset };
  return { offset, pageSize };
}

export function buildGraphPageUrl({
  baseUrl,
  repoPath,
  cypherQuery,
  globalScope = false,
  offset = 0,
  pageSize,
}) {
  const url = new URL("/api/graph", baseUrl);
  if (repoPath) url.searchParams.set("repo_path", repoPath);
  if (globalScope) url.searchParams.set("global_scope", "true");
  if (cypherQuery) url.searchParams.set("cypher_query", cypherQuery);
  url.searchParams.set("offset", String(offset));
  if (pageSize != null) url.searchParams.set("page_size", String(pageSize));
  return url;
}

export async function fetchGraphPage({
  baseUrl,
  repoPath,
  cypherQuery,
  globalScope = false,
  offset = 0,
  pageSize,
  fetchImpl = fetch,
}) {
  let request = { baseUrl, repoPath, cypherQuery, globalScope, offset, pageSize };
  while (true) {
    const url = buildGraphPageUrl(request);
    const response = await fetchImpl(url.toString());
    if (!response.ok) {
      const errorData = await response.json().catch(() => ({}));
      throw new Error(errorData.detail || `Server error (${response.status})`);
    }

    const page = await response.json();
    if (
      page.retry_required === true
      && Number.isInteger(page.retry_offset)
      && Number.isInteger(page.retry_page_size)
      && page.retry_page_size > 0
      && page.retry_page_size < (request.pageSize ?? Number.MAX_SAFE_INTEGER)
    ) {
      request = {
        ...request,
        offset: page.retry_offset,
        pageSize: page.retry_page_size,
      };
      continue;
    }
    return page;
  }
}

export async function fetchFullGraphFile(baseUrl, path, fetchImpl = fetch) {
  const pageSize = 8_000;
  const content = [];
  let offset = 0;
  while (true) {
    const url = new URL("/api/file", baseUrl);
    url.searchParams.set("path", path);
    url.searchParams.set("offset", String(offset));
    url.searchParams.set("page_size", String(pageSize));
    const response = await fetchImpl(url.toString());
    if (!response.ok) {
      const errorData = await response.json().catch(() => ({}));
      throw new Error(errorData.detail || `Server error (${response.status})`);
    }
    const result = await response.json();
    if (typeof result.content !== "string") {
      throw new Error("The file endpoint did not return source text.");
    }
    content.push(result.content);
    if (result.continuation_limit_reached) {
      throw new Error("The source file exceeds the supported retrieval window.");
    }
    if (result.next_offset == null) {
      return content.join("");
    }
    if (!Number.isInteger(result.next_offset) || result.next_offset <= offset) {
      throw new Error("The file endpoint returned an invalid continuation offset.");
    }
    offset = result.next_offset;
  }
}

export async function loadGraphFileContent(path, preview, isTruncated, fetchFullFile) {
  if ((isTruncated || preview == null) && typeof fetchFullFile === "function") {
    return fetchFullFile(path);
  }
  return preview ?? null;
}

const graphRecordKey = (record, kind) => {
  if (record?.id != null) return `${kind}:${record.id}`;
  if (kind === "link") {
    return `${kind}:${record?.source ?? ""}:${record?.target ?? ""}:${record?.type ?? ""}`;
  }
  return `${kind}:${JSON.stringify(record)}`;
};

const mergeRecords = (first = [], second = [], kind) => {
  const seen = new Set();
  return [...first, ...second].filter((record) => {
    const key = graphRecordKey(record, kind);
    if (seen.has(key)) return false;
    seen.add(key);
    return true;
  });
};

export function mergeGraphPage(current, page) {
  if (!current) return page;
  return {
    ...current,
    ...page,
    nodes: mergeRecords(current.nodes, page.nodes, "node"),
    links: mergeRecords(current.links, page.links, "link"),
    files: [...new Set([...(current.files || []), ...(page.files || [])])].sort(),
    fileContents: { ...(current.fileContents || {}), ...(page.fileContents || {}) },
    fileContentsTruncated: {
      ...(current.fileContentsTruncated || {}),
      ...(page.fileContentsTruncated || {}),
    },
    offset: current.offset ?? page.offset,
    record_count: (current.record_count || 0) + (page.record_count || 0),
    truncation_reasons: page.truncation_reasons || [],
  };
}
