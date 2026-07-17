const { contextBridge } = require("electron");

contextBridge.exposeInMainWorld("dbSyncDesktop", {
  platform: process.platform,
  isDesktop: true,
});
