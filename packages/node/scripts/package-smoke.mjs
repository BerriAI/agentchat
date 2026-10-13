import { mkdtemp, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { spawnSync } from "node:child_process";

const directory = await mkdtemp(join(tmpdir(), "agentchat-npm-smoke-"));
const npm = process.env.npm_execpath;
if (!npm) throw new Error("Run this check with npm run smoke");
function run(args, cwd) {
  const result = spawnSync(process.execPath, [npm, ...args], { cwd, encoding: "utf8" });
  if (result.status !== 0) throw new Error(result.stderr || result.stdout);
  return result.stdout;
}
try {
  const packed = JSON.parse(run(["pack", "--json", "--ignore-scripts", "--pack-destination", directory], process.cwd()));
  const tarball = resolve(directory, packed[0].filename);
  await writeFile(join(directory, "package.json"), JSON.stringify({ private: true, type: "module" }));
  run(["install", "--ignore-scripts", "--no-audit", "--no-fund", tarball], directory);
  const script = `import { AgentChat, Slack } from '@berriai/agentchat';
    const app = new AgentChat({channels: [new Slack({botToken:'fixture',appToken:'fixture'})]});
    await app.close(); console.log('Installed native package loaded successfully');`;
  await writeFile(join(directory, "smoke.mjs"), script);
  const result = spawnSync(process.execPath, [join(directory, "smoke.mjs")], { cwd: directory, encoding: "utf8", timeout: 10000 });
  if (result.status !== 0) throw new Error(result.stderr || result.stdout);
  console.log(result.stdout.trim());
} finally {
  await rm(directory, { recursive: true, force: true });
}
