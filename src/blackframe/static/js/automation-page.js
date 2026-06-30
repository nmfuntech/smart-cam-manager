import { createAutomationController } from "./automation-ui.js";

const ctrl = createAutomationController({
  feedback: document.getElementById("auto-feedback"),
  statusPill: document.getElementById("auto-status-pill"),
  toggleCheckbox: document.getElementById("auto-toggle"),
  tabButtons: document.querySelectorAll(".auto-tab"),
  panelDevices: document.getElementById("panel-devices"),
  panelRules: document.getElementById("panel-rules"),
  deviceList: document.getElementById("device-list"),
  ruleList: document.getElementById("rule-list"),
  addDeviceBtn: document.getElementById("btn-add-device"),
  addRuleBtn: document.getElementById("btn-add-rule"),
  deviceDialog: document.getElementById("auto-device-dialog"),
  deviceDialogTitle: document.getElementById("device-dialog-title"),
  deviceFeedback: document.getElementById("device-feedback"),
  saveDeviceBtn: document.getElementById("btn-device-save"),
  closeDeviceBtns: document.querySelectorAll("#btn-device-close, #btn-device-cancel"),
  ruleDialog: document.getElementById("auto-rule-dialog"),
  ruleDialogTitle: document.getElementById("rule-dialog-title"),
  ruleFeedback: document.getElementById("rule-feedback"),
  saveRuleBtn: document.getElementById("btn-rule-save"),
  closeRuleBtns: document.querySelectorAll("#btn-rule-close, #btn-rule-cancel"),
  addActionBtn: document.getElementById("btn-add-action"),
  ruleActionsList: document.getElementById("rule-actions-list"),
  // rename
  renameDialog: document.getElementById("auto-rename-dialog"),
  renameFeedback: document.getElementById("rename-feedback"),
  renameSaveBtn: document.getElementById("btn-rename-save"),
  closeRenameBtns: document.querySelectorAll("#btn-rename-close, #btn-rename-cancel"),
  // wizard
  wizardBtn: document.getElementById("btn-wizard"),
  wizardDialog: document.getElementById("auto-wizard-dialog"),
  wizardFeedback: document.getElementById("wizard-feedback"),
  wizardPreview: document.getElementById("wizard-preview"),
  wizardScanBtn: document.getElementById("btn-wizard-scan"),
  wizardUploadBtn: document.getElementById("btn-wizard-upload"),
  wizardCommitBtn: document.getElementById("btn-wizard-commit"),
  wizardDevicesFile: document.getElementById("wizard-devices-file"),
  wizardSnapshotFile: document.getElementById("wizard-snapshot-file"),
  closeWizardBtns: document.querySelectorAll("#btn-wizard-close, #btn-wizard-cancel"),
  // import / export
  exportBtn: document.getElementById("btn-export"),
  importBtn: document.getElementById("btn-import"),
  importFile: document.getElementById("import-file"),
});

ctrl.init();
