import { createCameraConfigController } from "./camera-ui.js";

const cameras = createCameraConfigController({
  wifiPill: document.getElementById("wifi-pill"),
  feedback: document.getElementById("camera-config-feedback"),
  activeSummary: document.getElementById("camera-active-summary"),
  profileList: document.getElementById("camera-profile-list"),
  form: document.getElementById("camera-form"),
  formTitle: document.getElementById("camera-form-title"),
  openViewerOnSave: true,
});

cameras.bind();
cameras.refresh();
