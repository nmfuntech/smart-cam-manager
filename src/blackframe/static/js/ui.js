export function setPillState(element, text, state) {
  element.textContent = text;
  element.classList.remove("ok", "error", "active");
  if (state) {
    element.classList.add(state);
  }
}
