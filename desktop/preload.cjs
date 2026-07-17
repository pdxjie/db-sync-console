const { contextBridge } = require("electron");

contextBridge.exposeInMainWorld("dbSyncDesktop", {
  platform: process.platform,
  isDesktop: true,
  apiBase: process.argv.find((arg) => arg.startsWith("--db-sync-api="))?.replace("--db-sync-api=", "") || "",
});
