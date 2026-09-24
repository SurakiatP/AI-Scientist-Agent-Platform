import { spawn } from "node:child_process";
import { createDemoServer } from "./server.mjs";

const api = createDemoServer();
api.on("error", (error) => {
  console.error(`Demo fixture API could not start: ${error.message}`);
  process.exitCode = 1;
});
api.listen(18787, "127.0.0.1", () => {
  console.log("DEMO ONLY · fixture API at http://127.0.0.1:18787");
  const web = spawn("pnpm", ["exec", "next", "dev", "--hostname", "127.0.0.1"], {
    cwd: new URL("..", import.meta.url),
    env: {
      ...process.env,
      NODE_ENV: "development",
      NEXT_PUBLIC_SCILAB_DEMO: "1",
      SCILAB_API_ORIGIN: "http://127.0.0.1:18787",
    },
    stdio: "inherit",
  });
  const stop = () => {
    web.kill("SIGTERM");
    api.close();
  };
  process.once("SIGINT", stop);
  process.once("SIGTERM", stop);
  web.on("exit", (code) => {
    api.close();
    process.exitCode = code || 0;
  });
});
