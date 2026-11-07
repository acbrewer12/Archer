import express from "express";
import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { StreamableHTTPServerTransport } from "@modelcontextprotocol/sdk/server/streamableHttp.js";
import { z } from "zod";
import { execFile } from "node:child_process";
import { promisify } from "node:util";
import { mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import { chromium } from "playwright";

const execFileP = promisify(execFile);

const REPO = "acbrewer12/archer";
const GITHUB_TOKEN = process.env.GITHUB_TOKEN;
const HF_BASE = "https://aydencatman-archer.hf.space";
// test_archer.py has 6 known pre-existing failures in these classes (see repo's
// CLAUDE.md §8) that are unrelated to any given change. Don't report them as new.
const KNOWN_FAILING_CLASSES = ["TestRegisterDevice", "TestDeviceTierEndpoint"];

function gh(pathname, opts = {}) {
  if (!GITHUB_TOKEN) {
    throw new Error("GITHUB_TOKEN is not set on this server — add it in Render's dashboard.");
  }
  return fetch(`https://api.github.com/repos/${REPO}${pathname}`, {
    ...opts,
    headers: {
      Authorization: `Bearer ${GITHUB_TOKEN}`,
      Accept: "application/vnd.github+json",
      "User-Agent": "archer-gstack-mcp",
      ...(opts.body ? { "Content-Type": "application/json" } : {}),
      ...opts.headers,
    },
  });
}

async function run(cmd, args, cwd, timeoutMs = 10 * 60 * 1000) {
  try {
    const { stdout, stderr } = await execFileP(cmd, args, {
      cwd,
      timeout: timeoutMs,
      maxBuffer: 20 * 1024 * 1024,
    });
    return { ok: true, stdout, stderr };
  } catch (e) {
    return { ok: false, stdout: e.stdout ?? "", stderr: e.stderr ?? String(e) };
  }
}

async function cloneBranch(branch) {
  const dir = await mkdtemp(path.join(tmpdir(), "archer-"));
  const cloneUrl = GITHUB_TOKEN
    ? `https://x-access-token:${GITHUB_TOKEN}@github.com/${REPO}.git`
    : `https://github.com/${REPO}.git`;
  const res = await run("git", ["clone", "--depth", "1", "--branch", branch, cloneUrl, dir]);
  if (!res.ok) throw new Error(`clone of branch '${branch}' failed:\n${res.stderr}`);
  return dir;
}

async function cleanup(dir) {
  await rm(dir, { recursive: true, force: true }).catch(() => {});
}

function extractFailures(output) {
  const failures = [];
  for (const line of output.split("\n")) {
    const m = line.match(/^FAILED (\S+)/);
    if (m) failures.push(m[1]);
  }
  return failures;
}

async function runSuite(dir) {
  const venv = path.join(dir, ".venv");
  await run("python3", ["-m", "venv", venv], dir);
  const pip = path.join(venv, "bin", "pip");
  const py = path.join(venv, "bin", "python3");
  const install = await run(pip, ["install", "-q", "-r", "requirements.txt"], dir);
  if (!install.ok) return { ok: false, output: `dependency install failed:\n${install.stderr.slice(-4000)}` };
  const test = await run(py, ["-m", "pytest", "test_archer.py", "test_fcm_push.py", "-q"], dir);
  const output = test.stdout + "\n" + test.stderr;
  const newFailures = extractFailures(output).filter(
    (name) => !KNOWN_FAILING_CLASSES.some((k) => name.includes(k))
  );
  return { ok: test.ok, newFailures, tail: output.split("\n").slice(-15).join("\n") };
}

const server = new McpServer({ name: "archer-gstack", version: "1.0.0" });

server.registerTool(
  "run_tests",
  {
    title: "Run Archer's test suite",
    description:
      "Clones the given branch of acbrewer12/archer fresh and runs `python3 -m pytest test_archer.py test_fcm_push.py -q`. Reports pass/fail and flags any NEW failures beyond the 6 known pre-existing ones in TestRegisterDevice/TestDeviceTierEndpoint.",
    inputSchema: { branch: z.string().describe("Git branch name to test") },
  },
  async ({ branch }) => {
    const dir = await cloneBranch(branch);
    try {
      const result = await runSuite(dir);
      return {
        content: [
          {
            type: "text",
            text: `branch: ${branch}\nnew failures beyond the 6 known pre-existing ones: ${
              result.newFailures?.length ? result.newFailures.join(", ") : "none"
            }\n\n--- tail of pytest output ---\n${result.tail ?? result.output}`,
          },
        ],
        isError: !result.ok && result.newFailures?.length > 0,
      };
    } finally {
      await cleanup(dir);
    }
  }
);

server.registerTool(
  "ship",
  {
    title: "Open or update a PR for a branch",
    description:
      "Runs the test suite on the given branch, then opens a PR from that branch into main if one doesn't already exist. Never pushes or merges to main directly — that's land_and_deploy, a separate explicit step.",
    inputSchema: {
      branch: z.string().describe("Branch to ship (must already be pushed to origin)"),
      title: z.string().describe("PR title"),
      body: z.string().optional().describe("PR description"),
    },
  },
  async ({ branch, title, body }) => {
    const dir = await cloneBranch(branch);
    let testTail = "";
    try {
      const result = await runSuite(dir);
      testTail = result.tail ?? result.output ?? "";
    } finally {
      await cleanup(dir);
    }

    const owner = REPO.split("/")[0];
    const existing = await gh(`/pulls?head=${owner}:${branch}&state=open`);
    const existingPRs = await existing.json();
    if (Array.isArray(existingPRs) && existingPRs.length > 0) {
      return {
        content: [
          { type: "text", text: `PR already open: ${existingPRs[0].html_url}\n\n--- test tail ---\n${testTail}` },
        ],
      };
    }

    const prRes = await gh(`/pulls`, {
      method: "POST",
      body: JSON.stringify({ title, head: branch, base: "main", body: body ?? "" }),
    });
    const pr = await prRes.json();
    if (!prRes.ok) {
      return { content: [{ type: "text", text: `failed to open PR: ${JSON.stringify(pr)}` }], isError: true };
    }
    return { content: [{ type: "text", text: `opened PR: ${pr.html_url}\n\n--- test tail ---\n${testTail}` }] };
  }
);

server.registerTool(
  "land_and_deploy",
  {
    title: "Merge a PR into main",
    description:
      "Merges an already-open PR into main (squash merge). Refuses if the PR has merge conflicts. Merging to main triggers the repo's existing sync-to-hf GitHub Action, which mirrors to the HuggingFace Space — this tool does not touch the Pi or the truck.",
    inputSchema: { pr_number: z.number().describe("PR number to merge") },
  },
  async ({ pr_number }) => {
    const prRes = await gh(`/pulls/${pr_number}`);
    const pr = await prRes.json();
    if (!prRes.ok) {
      return { content: [{ type: "text", text: `couldn't read PR #${pr_number}: ${JSON.stringify(pr)}` }], isError: true };
    }
    if (pr.mergeable === false) {
      return { content: [{ type: "text", text: `PR #${pr_number} has conflicts — not merging.` }], isError: true };
    }
    const mergeRes = await gh(`/pulls/${pr_number}/merge`, {
      method: "PUT",
      body: JSON.stringify({ merge_method: "squash" }),
    });
    const mergeJson = await mergeRes.json();
    if (!mergeRes.ok) {
      return { content: [{ type: "text", text: `merge failed: ${JSON.stringify(mergeJson)}` }], isError: true };
    }
    return {
      content: [
        {
          type: "text",
          text: `merged PR #${pr_number} into main (${mergeJson.sha}). The sync-to-hf workflow should pick it up — check https://github.com/${REPO}/actions and ${HF_BASE} in a minute or two.`,
        },
      ],
    };
  }
);

server.registerTool(
  "qa_browser",
  {
    title: "Browser-QA the live HuggingFace mirror",
    description:
      "Opens a real headless browser against the read-only HuggingFace Space mirror (no relay/vehicle control) and loads the given paths, reporting console errors, failed network requests, and page status per page.",
    inputSchema: {
      paths: z
        .array(z.string())
        .default(["/display", "/passenger", "/simulator", "/valet", "/fans"])
        .describe("Paths on the HF space to visit, e.g. ['/display']"),
    },
  },
  async ({ paths }) => {
    const browser = await chromium.launch();
    const results = [];
    try {
      for (const p of paths) {
        const page = await browser.newPage();
        const consoleErrors = [];
        const failedRequests = [];
        page.on("console", (msg) => {
          if (msg.type() === "error") consoleErrors.push(msg.text());
        });
        page.on("requestfailed", (req) => {
          failedRequests.push(`${req.method()} ${req.url()} — ${req.failure()?.errorText}`);
        });
        try {
          const resp = await page.goto(HF_BASE + p, { waitUntil: "networkidle", timeout: 20000 });
          results.push({
            path: p,
            status: resp?.status(),
            consoleErrors: consoleErrors.slice(0, 20),
            failedRequests: failedRequests.slice(0, 20),
          });
        } catch (e) {
          results.push({ path: p, error: String(e) });
        } finally {
          await page.close();
        }
      }
    } finally {
      await browser.close();
    }
    return { content: [{ type: "text", text: JSON.stringify(results, null, 2) }] };
  }
);

server.registerTool(
  "retro",
  {
    title: "Weekly-style retro of recent work",
    description:
      "Summarizes commits on main over the last N days so you can spot what shipped and what recurred, for a quick retrospective.",
    inputSchema: { days: z.number().default(7).describe("How many days back to look") },
  },
  async ({ days }) => {
    const since = new Date(Date.now() - days * 86400000).toISOString();
    const res = await gh(`/commits?sha=main&since=${since}&per_page=100`);
    const commits = await res.json();
    if (!res.ok || !Array.isArray(commits)) {
      return { content: [{ type: "text", text: `failed to read commits: ${JSON.stringify(commits)}` }], isError: true };
    }
    const lines = commits.map((c) => `${c.sha.slice(0, 7)} ${c.commit.message.split("\n")[0]}`);
    return {
      content: [{ type: "text", text: `${commits.length} commit(s) on main in the last ${days} day(s):\n${lines.join("\n")}` }],
    };
  }
);

const app = express();
app.use(express.json());

const SECRET = process.env.MCP_PATH_SECRET;
if (!SECRET) {
  console.error("MCP_PATH_SECRET is not set — refusing to start.");
  process.exit(1);
}

app.get("/health", (_req, res) => res.send("ok"));

// Stateless mode: a fresh transport per request, per the MCP SDK's documented
// pattern for simple remote servers with no need to share session state.
app.post(`/mcp/${SECRET}`, async (req, res) => {
  const transport = new StreamableHTTPServerTransport({ sessionIdGenerator: undefined });
  res.on("close", () => transport.close());
  await server.connect(transport);
  await transport.handleRequest(req, res, req.body);
});

const PORT = process.env.PORT || 3000;
app.listen(PORT, () => console.log(`listening on ${PORT}`));
