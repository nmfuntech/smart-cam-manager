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
});

ctrl.init();
