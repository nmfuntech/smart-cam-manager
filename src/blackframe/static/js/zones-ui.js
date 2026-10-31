// Editor delle zone di esclusione del movimento (Impostazioni → Zone).
// Le coordinate sono normalizzate 0..1, così valgono a qualsiasi risoluzione.
import { fetchJson } from "./api.js";

const editor = document.getElementById("zone-editor");
if (editor) {
  const img = document.getElementById("zone-snapshot");
  const canvas = document.getElementById("zone-canvas");
  const feedback = document.getElementById("zone-feedback");
  const ctx = canvas.getContext("2d");
  let zones = [];
  let current = [];

  const say = (text, isError = false) => {
    feedback.textContent = text;
    feedback.classList.toggle("error", isError);
  };

  const resize = () => {
    canvas.width = img.clientWidth;
    canvas.height = img.clientHeight;
    draw();
  };

  const toPx = ([x, y]) => [x * canvas.width, y * canvas.height];

  const drawPolygon = (points, closed) => {
    if (!points.length) return;
    ctx.beginPath();
    points.map(toPx).forEach(([x, y], i) => (i ? ctx.lineTo(x, y) : ctx.moveTo(x, y)));
    if (closed) {
      ctx.closePath();
      ctx.fillStyle = "rgba(220, 40, 40, 0.35)";
      ctx.fill();
    }
    ctx.strokeStyle = closed ? "rgba(255, 80, 80, 0.95)" : "rgba(255, 210, 0, 0.95)";
    ctx.lineWidth = 2;
    ctx.stroke();
    points.map(toPx).forEach(([x, y]) => {
      ctx.fillStyle = ctx.strokeStyle;
      ctx.fillRect(x - 3, y - 3, 6, 6);
    });
  };

  const draw = () => {
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    zones.forEach((zone) => drawPolygon(zone, true));
    drawPolygon(current, false);
  };

  const closeCurrent = () => {
    if (current.length >= 3) {
      zones.push(current);
      say(`${zones.length} zone (non ancora salvate)`);
    } else if (current.length) {
      say("Servono almeno 3 punti per una zona", true);
      return;
    }
    current = [];
    draw();
  };

  const refreshImage = () => {
    img.src = `/snapshot.jpg?t=${Date.now()}`;
  };

  canvas.addEventListener("click", (event) => {
    const rect = canvas.getBoundingClientRect();
    const x = Math.min(1, Math.max(0, (event.clientX - rect.left) / rect.width));
    const y = Math.min(1, Math.max(0, (event.clientY - rect.top) / rect.height));
    current.push([Number(x.toFixed(4)), Number(y.toFixed(4))]);
    draw();
  });
  canvas.addEventListener("dblclick", (event) => {
    event.preventDefault();
    current.pop(); // il doppio clic aggiunge un punto in più
    closeCurrent();
  });
  document.getElementById("zone-close").addEventListener("click", closeCurrent);
  document.getElementById("zone-undo").addEventListener("click", () => {
    if (current.length) current.pop();
    else zones.pop();
    draw();
  });
  document.getElementById("zone-clear").addEventListener("click", () => {
    zones = [];
    current = [];
    draw();
    say("Tutte le zone rimosse (salva per confermare)");
  });
  document.getElementById("zone-refresh").addEventListener("click", refreshImage);
  document.getElementById("zone-save").addEventListener("click", async () => {
    if (current.length) closeCurrent();
    try {
      const data = await fetchJson("/api/motion_zones", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ exclude: zones }),
      });
      if (!data.ok) throw new Error(data.error || "Salvataggio non riuscito");
      zones = data.exclude;
      draw();
      say(zones.length ? `Salvate ${zones.length} zone: attive entro pochi secondi` : "Nessuna zona attiva");
    } catch (error) {
      say(error.message, true);
    }
  });

  img.addEventListener("load", resize);
  window.addEventListener("resize", resize);
  fetchJson("/api/motion_zones")
    .then((data) => {
      zones = data.exclude || [];
      draw();
    })
    .catch((error) => say(error.message, true));
  refreshImage();
}
