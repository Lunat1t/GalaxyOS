import { spawn } from "node:child_process";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const webDir = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const repoRoot = resolve(webDir, "..");
const pythonScript = resolve(repoRoot, "galaxy.py");
const apiPort = Number(process.env.GALAXY_API_PORT || 8765);
const webPort = Number(process.env.PORT || 3000);
if (!Number.isInteger(apiPort) || apiPort < 1 || apiPort > 65535) {
  console.error("GALAXY_API_PORT must be between 1 and 65535");
  process.exit(2);
}
if (!Number.isInteger(webPort) || webPort < 1 || webPort > 65535) {
  console.error("PORT must be between 1 and 65535");
  process.exit(2);
}

const python = process.env.GALAXY_PYTHON || (process.platform === "win32" ? "py" : "python3");
const pythonArgs = python.toLowerCase() === "py"
  ? ["-3", "-u", pythonScript, "serve"]
  : ["-u", pythonScript, "serve"];
pythonArgs.push("--port", String(apiPort));
const environment = {
  ...process.env,
  PYTHONUNBUFFERED: "1",
  GALAXY_API_PORT: String(apiPort),
  GALAXY_API_URL: `http://127.0.0.1:${apiPort}`,
};

let stopping = false;
let exitCode = 0;
let apiExited = false;
let nextExited = true;
let nextProcess;
let timeout;

function finishIfStopped() {
  if (stopping && apiExited && nextExited) {
    if (timeout) clearTimeout(timeout);
    process.exit(exitCode);
  }
}

function stop(code = 0) {
  if (stopping) return;
  stopping = true;
  exitCode = code;
  if (apiProcess && !apiExited) apiProcess.kill("SIGINT");
  if (nextProcess && !nextExited) nextProcess.kill("SIGTERM");
  timeout = setTimeout(() => {
    if (!apiExited) apiProcess.kill();
    if (!nextExited) nextProcess.kill();
    process.exit(exitCode || 1);
  }, 5000);
  timeout.unref();
  finishIfStopped();
}

let apiProcess;
try {
  apiProcess = spawn(python, pythonArgs, {
    cwd: repoRoot,
    env: environment,
    stdio: ["inherit", "pipe", "inherit"],
  });
} catch (error) {
  console.error(`Could not start Python (${python}): ${error.message}`);
  process.exit(1);
}

apiProcess.on("error", (error) => {
  console.error(`Could not start Python (${python}): ${error.message}`);
  apiExited = true;
  stop(1);
  finishIfStopped();
});

apiProcess.stdout.setEncoding("utf8");
let apiOutputTail = "";
apiProcess.stdout.on("data", (chunk) => {
  process.stdout.write(chunk);
  apiOutputTail = `${apiOutputTail}${chunk}`.slice(-256);
  if (!nextProcess && !stopping && apiOutputTail.includes("Galaxy local API listening at")) {
    const nextBin = resolve(webDir, "node_modules/next/dist/bin/next");
    nextExited = false;
    nextProcess = spawn(process.execPath, [nextBin, "dev", "--webpack", "--hostname", "127.0.0.1", "--port", String(webPort)], {
      cwd: webDir,
      env: { ...environment, NEXT_TELEMETRY_DISABLED: "1" },
      stdio: "inherit",
    });
    nextProcess.on("error", (error) => {
      console.error(`Could not start Next.js: ${error.message}`);
      nextExited = true;
      stop(1);
    });
    nextProcess.on("exit", (code, signal) => {
      nextExited = true;
      if (!stopping) stop(code ?? (signal ? 1 : 0));
      finishIfStopped();
    });
    console.log(`Starting Galaxy web workspace at http://127.0.0.1:${webPort}`);
  }
});

apiProcess.on("exit", (code, signal) => {
  apiExited = true;
  if (!stopping) {
    if (!nextProcess) console.error("Galaxy API stopped before it became ready.");
    stop(code ?? (signal ? 1 : 0));
  }
  finishIfStopped();
});

process.on("SIGINT", () => stop(0));
process.on("SIGTERM", () => stop(0));
