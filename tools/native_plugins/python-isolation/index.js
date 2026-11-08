// GPL-3.0-or-later. Native V2 shell-environment policy.
import { mkdtemp, chmod } from "node:fs/promises"
import os from "node:os"
import path from "node:path"

export default {
  id: "python-isolation",
  async setup(ctx) {
    const cache = await mkdtemp(path.join(os.tmpdir(), "opencode-python-"))
    await chmod(cache, 0o700)
    await ctx.shell.hook("create.before", event => {
      Object.assign(event.env, {
        PIP_REQUIRE_VIRTUALENV: "1", PIP_DISABLE_PIP_VERSION_CHECK: "1", PIP_NO_INPUT: "1",
        UV_CACHE_DIR: path.join(cache, "uv"), PIP_CACHE_DIR: path.join(cache, "pip"),
        UV_PYTHON_INSTALL_DIR: path.join(cache, "python"), UV_TOOL_DIR: path.join(cache, "tools"),
        PYTHONDONTWRITEBYTECODE: "1",
      })
    })
    // Cache contains package state, not credentials. Preserve it on reload;
    // system temporary storage handles eventual expiry, not recursive deletes.
  },
}
