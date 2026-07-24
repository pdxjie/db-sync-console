const { app, BrowserWindow, Menu, dialog, shell } = require("electron");
const { spawn } = require("child_process");
const fs = require("fs");
const http = require("http");
const net = require("net");
const path = require("path");

let mainWindow = null;
let backendProcess = null;
let backendUrl = null;

const isMac = process.platform === "darwin";
const APP_NAME = "同步犬";

function appIconPath() {
  return path.join(appRootPath(), "assets", "app-icon.icns");
}

function dockIconPath() {
  return path.join(appRootPath(), "assets", "app-icon.png");
}
function appRootPath() {
  if (app.isPackaged) {
    return path.join(process.resourcesPath, "app");
  }
  return path.resolve(__dirname, "..");
}

function userDataPath(...parts) {
  return path.join(app.getPath("userData"), ...parts);
}

function backendExecutableName() {
  return process.platform === "win32" ? "syncdog-backend.exe" : "syncdog-backend";
}

function backendRuntimeCandidates() {
  const appRoot = appRootPath();
  const executableName = backendExecutableName();
  const arch = process.arch === "x64" ? "x64" : process.arch;
  const candidates = [];
  if (process.env.DB_SYNC_BACKEND_BINARY) {
    candidates.push(process.env.DB_SYNC_BACKEND_BINARY);
  }
  candidates.push(path.join(appRoot, "desktop", "backend-dist", `${process.platform}-${arch}`, "syncdog-backend", executableName));
  if (process.platform === "darwin") {
    candidates.push(path.join(appRoot, "desktop", "backend-dist", "darwin-arm64", "syncdog-backend", executableName));
    candidates.push(path.join(appRoot, "desktop", "backend-dist", "darwin-x64", "syncdog-backend", executableName));
  }
  candidates.push(path.join(appRoot, "desktop", "backend-dist", "syncdog-backend", executableName));
  return [...new Set(candidates)];
}

function findBackendRuntime() {
  return backendRuntimeCandidates().find((candidate) => fs.existsSync(candidate));
}

function findPython() {
  if (process.env.DB_SYNC_PYTHON) {
    return process.env.DB_SYNC_PYTHON;
  }
  const appRoot = appRootPath();
  const venvPython = path.join(appRoot, ".venv", "bin", "python");
  if (fs.existsSync(venvPython)) {
    return venvPython;
  }
  return "python3";
}

function pythonCommand() {
  const python = findPython();
  if (process.platform === "darwin" && fs.existsSync("/usr/bin/arch")) {
    return {
      command: "/usr/bin/arch",
      args: ["-arm64", python],
    };
  }
  return {
    command: python,
    args: [],
  };
}

function backendCommand() {
  const runtime = findBackendRuntime();
  if (runtime) {
    return {
      command: runtime,
      args: [],
      kind: "runtime",
    };
  }
  if (app.isPackaged) {
    throw new Error(
      `Packaged backend runtime was not found. Looked in: ${backendRuntimeCandidates().join(", ")}`
    );
  }
  const python = pythonCommand();
  return {
    command: python.command,
    args: [...python.args, "-m", "sync_tool.cli"],
    kind: "python",
  };
}

function findFreePort() {
  return new Promise((resolve, reject) => {
    const server = net.createServer();
    server.once("error", reject);
    server.listen(0, "127.0.0.1", () => {
      const address = server.address();
      const port = address.port;
      server.close(() => resolve(port));
    });
  });
}

function waitForBackend(url, timeoutMs = 30000) {
  const deadline = Date.now() + timeoutMs;
  return new Promise((resolve, reject) => {
    const check = () => {
      const request = http.get(`${url}/api/status`, (response) => {
        response.resume();
        if (response.statusCode >= 200 && response.statusCode < 300) {
          resolve();
          return;
        }
        retry(new Error(`Backend returned ${response.statusCode}`));
      });
      request.on("error", retry);

      function retry(error) {
        if (Date.now() > deadline) {
          reject(error);
          return;
        }
        setTimeout(check, 350);
      }
    };
    check();
  });
}

function startBackend(port) {
  const backend = backendCommand();
  const appRoot = appRootPath();
  const env = {
    ...process.env,
    DB_SYNC_DESKTOP: "1",
    DB_SYNC_DATA_DIR: userDataPath("data"),
    DB_SYNC_LOG_DIR: userDataPath("logs"),
    PYTHONUNBUFFERED: "1",
  };
  console.log(`[desktop] starting backend via ${backend.kind}: ${backend.command}`);
  backendProcess = spawn(
    backend.command,
    [...backend.args, "serve", "--host", "127.0.0.1", "--port", String(port)],
    {
      cwd: appRoot,
      env,
      stdio: ["ignore", "pipe", "pipe"],
    }
  );

  backendProcess.stdout.on("data", (chunk) => {
    console.log(`[backend] ${chunk.toString().trim()}`);
  });
  backendProcess.stderr.on("data", (chunk) => {
    console.error(`[backend] ${chunk.toString().trim()}`);
  });
  backendProcess.on("error", (error) => {
    console.error(`[backend] failed to start: ${error.message}`);
  });
  backendProcess.on("exit", (code, signal) => {
    console.log(`[backend] exited code=${code} signal=${signal}`);
    backendProcess = null;
  });
}

function createMenu() {
  const template = [
    ...(isMac
      ? [
          {
            label: app.name,
            submenu: [
              { role: "about" },
              { type: "separator" },
              { role: "services" },
              { type: "separator" },
              { role: "hide" },
              { role: "hideOthers" },
              { role: "unhide" },
              { type: "separator" },
              { role: "quit" },
            ],
          },
        ]
      : []),
    {
      label: "File",
      submenu: [
        {
          label: "Open Logs Folder",
          click: () => shell.openPath(userDataPath("logs")),
        },
        {
          label: "Open Data Folder",
          click: () => shell.openPath(userDataPath("data")),
        },
        { type: "separator" },
        isMac ? { role: "close" } : { role: "quit" },
      ],
    },
    {
      label: "Edit",
      submenu: [
        { role: "undo" },
        { role: "redo" },
        { type: "separator" },
        { role: "cut" },
        { role: "copy" },
        { role: "paste" },
        ...(isMac ? [{ role: "pasteAndMatchStyle" }] : []),
        { role: "delete" },
        { type: "separator" },
        { role: "selectAll" },
      ],
    },
    {
      label: "View",
      submenu: [
        { role: "reload" },
        { role: "forceReload" },
        { role: "toggleDevTools" },
        { type: "separator" },
        { role: "resetZoom" },
        { role: "zoomIn" },
        { role: "zoomOut" },
        { type: "separator" },
        { role: "togglefullscreen" },
      ],
    },
    {
      label: "Help",
      submenu: [
        {
          label: "GitHub Repository",
          click: () => shell.openExternal("https://github.com/pdxjie/db-sync-console"),
        },
      ],
    },
  ];
  Menu.setApplicationMenu(Menu.buildFromTemplate(template));
}

function createWindow() {
  mainWindow = new BrowserWindow({
    width: 1440,
    height: 940,
    minWidth: 1180,
    minHeight: 760,
    show: false,
    title: APP_NAME,
    backgroundColor: "#f4f6f8",
    icon: appIconPath(),
    titleBarStyle: isMac ? "hiddenInset" : "default",
    trafficLightPosition: { x: 16, y: 18 },
    webPreferences: {
      preload: path.join(__dirname, "preload.cjs"),
      additionalArguments: [`--db-sync-api=${backendUrl}`],
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
    },
  });

  const rendererIndex = path.join(appRootPath(), "desktop", "renderer", "dist", "index.html");
  if (fs.existsSync(rendererIndex)) {
    mainWindow.loadFile(rendererIndex);
  } else {
    mainWindow.loadURL(backendUrl);
  }
  mainWindow.once("ready-to-show", () => {
    if (mainWindow) {
      mainWindow.show();
    }
  });
  mainWindow.webContents.on("context-menu", (event, params) => {
    const template = [];
    if (params.isEditable) {
      template.push(
        { role: "undo" },
        { role: "redo" },
        { type: "separator" },
        { role: "cut", enabled: params.editFlags.canCut },
        { role: "copy", enabled: params.editFlags.canCopy },
        { role: "paste", enabled: params.editFlags.canPaste },
        { role: "delete", enabled: params.editFlags.canDelete },
        { type: "separator" },
        { role: "selectAll", enabled: params.editFlags.canSelectAll }
      );
    } else if (params.selectionText) {
      template.push({ role: "copy" });
    }
    if (template.length > 0) {
      Menu.buildFromTemplate(template).popup({ window: mainWindow });
    }
  });
  mainWindow.on("closed", () => {
    mainWindow = null;
  });
}

async function boot() {
  app.setName(APP_NAME);
  if (isMac && fs.existsSync(dockIconPath())) {
    try {
      app.dock.setIcon(dockIconPath());
    } catch (error) {
      console.warn(`[desktop] unable to set dock icon: ${error.message}`);
    }
  }
  createMenu();
  const port = await findFreePort();
  backendUrl = `http://127.0.0.1:${port}`;
  try {
    startBackend(port);
    await waitForBackend(backendUrl);
    createWindow();
  } catch (error) {
    dialog.showErrorBox(
      `${APP_NAME} failed to start`,
      `Unable to start the local sync backend.\n\n${error.message}`
    );
    app.quit();
  }
}

app.whenReady().then(boot);

app.on("activate", () => {
  if (BrowserWindow.getAllWindows().length === 0 && backendUrl) {
    createWindow();
  }
});

app.on("before-quit", () => {
  if (backendProcess) {
    backendProcess.kill("SIGTERM");
    backendProcess = null;
  }
});

app.on("window-all-closed", () => {
  if (!isMac) {
    app.quit();
  }
});
