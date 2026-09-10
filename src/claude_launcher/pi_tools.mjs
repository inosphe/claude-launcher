// Tools claunch adds to every Pi session it launches.
//
//  - full_read: return a whole file with 1-based line numbers, never
//    truncated. Pi's built-in `read` caps its output (lines and bytes), which
//    is right for a quick look and wrong for a design document or a log the
//    model must see end to end. The tool refuses directories, missing files
//    and binaries, and reports the size it returned so the caller can judge
//    the context it just spent.
//
// CLAUNCH_PI_TOOLS names the tools to register (comma-separated); unset means
// all of them. The launcher sets it from `harness_options.pi.tools`.

import fs from "node:fs";
import path from "node:path";
import { Type } from "@sinclair/typebox";

const ALL_TOOLS = ["full_read"];

function enabledTools() {
  const raw = process.env.CLAUNCH_PI_TOOLS;
  if (raw === undefined || raw === "") return new Set(ALL_TOOLS);
  return new Set(
    raw
      .split(",")
      .map((s) => s.trim())
      .filter(Boolean),
  );
}

/** Whether the first bytes look like a binary file (a NUL within 8 KiB). */
export function looksBinary(buffer) {
  const head = buffer.subarray(0, Math.min(buffer.length, 8192));
  return head.includes(0);
}

/** Number every line, right-aligned, `N│text`. */
export function numberLines(text) {
  const lines = text.split(/\r?\n/);
  if (lines.length > 1 && lines[lines.length - 1] === "") lines.pop();
  const width = String(lines.length).length;
  const body = lines.map((l, i) => `${String(i + 1).padStart(width)}│${l}`).join("\n");
  return { body, count: lines.length };
}

/** The complete tool output for `file` (absolute, or relative to `cwd`). */
export function fullRead(file, cwd) {
  const p = path.resolve(cwd, file);
  let stat;
  try {
    stat = fs.statSync(p);
  } catch (err) {
    return { error: `No such file: ${file} (resolved to ${p})` };
  }
  if (stat.isDirectory()) {
    return { error: `${file} is a directory; full_read takes one file` };
  }
  const buffer = fs.readFileSync(p);
  if (looksBinary(buffer)) {
    return { error: `${file} looks binary (${buffer.length} bytes); full_read returns text only` };
  }
  const text = buffer.toString("utf8");
  const { body, count } = numberLines(text);
  const header = `# ${p} (${count} lines, ${buffer.length} bytes)`;
  return { text: `${header}\n${body}`, lines: count, bytes: buffer.length, path: p };
}

export default function (pi) {
  const enabled = enabledTools();
  if (!enabled.has("full_read")) return;

  pi.registerTool({
    name: "full_read",
    label: "Full Read",
    description:
      "Read an entire file without truncation. Returns the complete UTF-8 text " +
      "with 1-based line numbers, prefixed by a header with the line and byte " +
      "counts. Use it when a document or log must be seen end to end; the " +
      "built-in read tool caps its output. Text files only.",
    promptSnippet: "Read a whole file with line numbers, never truncated (text only)",
    promptGuidelines: [
      "Prefer full_read over read when a file must be understood end to end; the header tells you how much context it cost.",
    ],
    parameters: Type.Object({
      path: Type.String({
        description: "Path of the file to read, absolute or relative to the working directory.",
      }),
    }),
    async execute(_toolCallId, params, _signal, _onUpdate, ctx) {
      const cwd = (ctx && ctx.cwd) || process.cwd();
      const result = fullRead(String(params.path ?? ""), cwd);
      if (result.error) {
        return { content: [{ type: "text", text: result.error }], details: {}, isError: true };
      }
      return {
        content: [{ type: "text", text: result.text }],
        details: { path: result.path, lines: result.lines, bytes: result.bytes },
      };
    },
  });
}
