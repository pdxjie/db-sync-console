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
const APP_NAME = "Data Sync Studio";
function appRootPath() {
  if (app.isPackaged) {
    return path.join(process.resourcesPath, "app");
  }
  return path.resolve(__dirname, "..");
}

function userDataPath(...parts) {
  return path.join(app.getPath("userData"), ...parts);
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
  const python = pythonCommand();
  const appRoot = appRootPath();
  const env = {
    ...process.env,
    DB_SYNC_DESKTOP: "1",
    DB_SYNC_DATA_DIR: userDataPath("data"),
    DB_SYNC_LOG_DIR: userDataPath("logs"),
    PYTHONUNBUFFERED: "1",
  };
  backendProcess = spawn(
    python.command,
    [...python.args, "-m", "sync_tool.cli", "serve", "--host", "127.0.0.1", "--port", String(port)],
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
    title: APP_NAME,
    backgroundColor: "#f4f6f8",
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
  mainWindow.on("closed", () => {
    mainWindow = null;
  });
}

async function boot() {
  app.setName(APP_NAME);
  createMenu();
  const port = await findFreePort();
  backendUrl = `http://127.0.0.1:${port}`;
  startBackend(port);
  try {
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
