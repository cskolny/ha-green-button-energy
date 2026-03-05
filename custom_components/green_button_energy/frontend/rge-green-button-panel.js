/**
 * RG&E Green Button Import Panel
 *
 * A custom Home Assistant sidebar panel that provides drag-and-drop
 * file import for RG&E Green Button CSV and XML exports.
 *
 * Architecture:
 *   - Runs entirely in the browser as a native Web Component
 *   - Reads the dropped file as base64 in the browser
 *   - Sends content over the existing HA WebSocket connection
 *     using hass.connection.sendMessagePromise (no extra auth needed)
 *   - Backend WebSocket handler parses the file in memory and
 *     updates the sensors directly — no filesystem access required
 */

class GreenButtonEnergyPanel extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._hass = null;
    this._results = [];
  }

  set hass(hass) {
    this._hass = hass;
    if (!this._rendered) {
      this._render();
      this._rendered = true;
    }
  }

  set panel(panel) {
    this._panel = panel;
  }

  _render() {
    this.shadowRoot.innerHTML = `
      <style>
        :host {
          display: block;
          font-family: var(--paper-font-body1_-_font-family, 'Roboto', sans-serif);
          background: var(--primary-background-color, #f5f5f5);
          min-height: 100vh;
          box-sizing: border-box;
        }

        .page {
          max-width: 860px;
          margin: 0 auto;
          padding: 24px 16px 48px;
        }

        h1 {
          font-size: 1.6rem;
          font-weight: 400;
          color: var(--primary-text-color, #212121);
          margin: 0 0 4px;
          display: flex;
          align-items: center;
          gap: 10px;
        }

        h1 ha-icon {
          color: var(--primary-color, #03a9f4);
        }

        .subtitle {
          color: var(--secondary-text-color, #727272);
          font-size: 0.9rem;
          margin: 0 0 28px;
        }

        /* ── Drop zones ── */
        .zones {
          display: grid;
          grid-template-columns: 1fr 1fr;
          gap: 16px;
          margin-bottom: 24px;
        }

        @media (max-width: 600px) {
          .zones { grid-template-columns: 1fr; }
        }

        .drop-zone {
          border: 2px dashed var(--divider-color, #e0e0e0);
          border-radius: 12px;
          padding: 32px 20px;
          text-align: center;
          cursor: pointer;
          transition: border-color 0.2s, background 0.2s;
          background: var(--card-background-color, #fff);
          position: relative;
          user-select: none;
        }

        .drop-zone:hover,
        .drop-zone.dragover {
          border-color: var(--primary-color, #03a9f4);
          background: color-mix(in srgb, var(--primary-color, #03a9f4) 6%, transparent);
        }

        .drop-zone.processing {
          border-color: var(--warning-color, #ff9800);
          pointer-events: none;
        }

        .drop-zone .icon {
          font-size: 2.4rem;
          margin-bottom: 10px;
          display: block;
        }

        .drop-zone .label {
          font-size: 1rem;
          font-weight: 500;
          color: var(--primary-text-color, #212121);
          margin-bottom: 4px;
        }

        .drop-zone .hint {
          font-size: 0.8rem;
          color: var(--secondary-text-color, #727272);
        }

        .drop-zone input[type="file"] {
          position: absolute;
          inset: 0;
          opacity: 0;
          cursor: pointer;
          width: 100%;
          height: 100%;
        }

        /* ── Spinner ── */
        .spinner {
          width: 28px;
          height: 28px;
          border: 3px solid var(--divider-color, #e0e0e0);
          border-top-color: var(--primary-color, #03a9f4);
          border-radius: 50%;
          animation: spin 0.8s linear infinite;
          margin: 8px auto 0;
        }

        @keyframes spin {
          to { transform: rotate(360deg); }
        }

        /* ── Results log ── */
        .results-header {
          font-size: 0.85rem;
          font-weight: 500;
          color: var(--secondary-text-color, #727272);
          text-transform: uppercase;
          letter-spacing: 0.06em;
          margin-bottom: 10px;
          display: flex;
          align-items: center;
          justify-content: space-between;
        }

        .clear-btn {
          background: none;
          border: none;
          color: var(--primary-color, #03a9f4);
          font-size: 0.8rem;
          cursor: pointer;
          padding: 0;
        }

        .results-list {
          display: flex;
          flex-direction: column;
          gap: 10px;
        }

        .result-card {
          background: var(--card-background-color, #fff);
          border-radius: 10px;
          padding: 14px 16px;
          border-left: 4px solid transparent;
          box-shadow: 0 1px 3px rgba(0,0,0,0.08);
        }

        .result-card.success { border-left-color: var(--success-color, #4caf50); }
        .result-card.warning { border-left-color: var(--warning-color, #ff9800); }
        .result-card.error   { border-left-color: var(--error-color, #f44336); }

        .result-title {
          font-weight: 500;
          font-size: 0.92rem;
          color: var(--primary-text-color, #212121);
          margin-bottom: 4px;
          display: flex;
          align-items: center;
          gap: 6px;
        }

        .result-detail {
          font-size: 0.82rem;
          color: var(--secondary-text-color, #727272);
          line-height: 1.6;
        }

        .result-time {
          font-size: 0.75rem;
          color: var(--disabled-text-color, #bdbdbd);
          margin-top: 4px;
        }

        .stat {
          display: inline-block;
          background: var(--primary-background-color, #f5f5f5);
          border-radius: 4px;
          padding: 1px 7px;
          font-size: 0.8rem;
          margin-right: 4px;
        }

        .empty-state {
          text-align: center;
          padding: 32px;
          color: var(--secondary-text-color, #727272);
          font-size: 0.9rem;
          background: var(--card-background-color, #fff);
          border-radius: 10px;
        }
      </style>

      <div class="page">
        <h1>
          <ha-icon icon="mdi:lightning-bolt-circle"></ha-icon>
          Green Button Energy Import
        </h1>
        <p class="subtitle">
          Download your usage data from <strong>your utility website → My Energy Use → Download Data</strong>,
          then drop the CSV or XML file below.
        </p>

        <div class="zones">
          <!-- Electric drop zone -->
          <div class="drop-zone" id="zone-electric"
               data-service-type="electric" data-label="Electric">
            <input type="file" accept=".csv,.xml"
                   id="file-electric"
                   data-service-type="electric" />
            <span class="icon">⚡</span>
            <div class="label">Electric Usage</div>
            <div class="hint">Drop CSV or XML here, or click to browse</div>
          </div>

          <!-- Gas drop zone -->
          <div class="drop-zone" id="zone-gas"
               data-service-type="gas" data-label="Gas">
            <input type="file" accept=".csv,.xml"
                   id="file-gas"
                   data-service-type="gas" />
            <span class="icon">🔥</span>
            <div class="label">Gas Usage</div>
            <div class="hint">Drop CSV or XML here, or click to browse</div>
          </div>
        </div>

        <!-- Results log -->
        <div id="results-section" style="display:none">
          <div class="results-header">
            Import History
            <button class="clear-btn" id="clear-btn">Clear</button>
          </div>
          <div class="results-list" id="results-list"></div>
        </div>

        <div class="empty-state" id="empty-state">
          No imports yet — drop a file above to get started.
        </div>
      </div>
    `;

    this._setupEventListeners();
  }

  _setupEventListeners() {
    const root = this.shadowRoot;

    // Set up both drop zones
    ["electric", "gas"].forEach((type) => {
      const zone = root.getElementById(`zone-${type}`);
      const input = root.getElementById(`file-${type}`);

      // Drag-and-drop events on the zone
      zone.addEventListener("dragover", (e) => {
        e.preventDefault();
        zone.classList.add("dragover");
      });

      zone.addEventListener("dragleave", () => {
        zone.classList.remove("dragover");
      });

      zone.addEventListener("drop", (e) => {
        e.preventDefault();
        zone.classList.remove("dragover");
        const file = e.dataTransfer?.files?.[0];
        if (file) this._handleFile(file, type, zone);
      });

      // Click-to-browse via hidden file input
      input.addEventListener("change", (e) => {
        const file = e.target.files?.[0];
        if (file) this._handleFile(file, type, zone);
        // Reset so the same file can be re-dropped if needed
        input.value = "";
      });
    });

    // Clear button
    root.getElementById("clear-btn").addEventListener("click", () => {
      this._results = [];
      this._refreshResults();
    });
  }

  async _handleFile(file, serviceType, zone) {
    const ext = file.name.split(".").pop().toLowerCase();
    if (!["csv", "xml"].includes(ext)) {
      this._addResult({
        type: "error",
        filename: file.name,
        serviceType,
        message: `Unsupported file type ".${ext}". Please use .csv or .xml.`,
      });
      return;
    }

    // Show processing state
    zone.classList.add("processing");
    const originalHint = zone.querySelector(".hint").textContent;
    zone.querySelector(".hint").textContent = "Importing…";
    const spinner = document.createElement("div");
    spinner.className = "spinner";
    zone.appendChild(spinner);

    try {
      const content = await this._readFileAsText(file);

      // Send to HA backend via WebSocket
      const response = await this._hass.connection.sendMessagePromise({
        type: "green_button_energy/import_file",
        filename: file.name,
        content: content,
        service_type: serviceType,
      });

      if (response.success) {
        if (response.rows_imported === 0) {
          this._addResult({
            type: "warning",
            filename: file.name,
            serviceType,
            message: "File processed — no new data found (already up to date).",
            stats: response,
          });
        } else {
          this._addResult({
            type: "success",
            filename: file.name,
            serviceType,
            message: "Import successful!",
            stats: response,
          });
        }
      } else {
        this._addResult({
          type: "error",
          filename: file.name,
          serviceType,
          message: response.error || "Import failed.",
          stats: response,
        });
      }
    } catch (err) {
      this._addResult({
        type: "error",
        filename: file.name,
        serviceType,
        message: `Connection error: ${err.message || err}`,
      });
    } finally {
      zone.classList.remove("processing");
      zone.querySelector(".hint").textContent = originalHint;
      spinner.remove();
    }
  }

  _readFileAsText(file) {
    return new Promise((resolve, reject) => {
      const reader = new FileReader();
      reader.onload = (e) => resolve(e.target.result);
      reader.onerror = () => reject(new Error("Failed to read file"));
      reader.readAsText(file, "UTF-8");
    });
  }

  _addResult(result) {
    result.timestamp = new Date().toLocaleTimeString();
    this._results.unshift(result); // newest first
    if (this._results.length > 20) this._results.pop(); // keep last 20
    this._refreshResults();
  }

  _refreshResults() {
    const root = this.shadowRoot;
    const list = root.getElementById("results-list");
    const section = root.getElementById("results-section");
    const empty = root.getElementById("empty-state");

    if (this._results.length === 0) {
      section.style.display = "none";
      empty.style.display = "block";
      return;
    }

    section.style.display = "block";
    empty.style.display = "none";

    const icons = { success: "✅", warning: "⚠️", error: "❌" };

    list.innerHTML = this._results
      .map((r) => {
        const statsHtml =
          r.stats && r.stats.rows_imported > 0
            ? `
            <div style="margin-top:6px">
              <span class="stat">📥 ${r.stats.rows_imported} rows</span>
              <span class="stat">📊 ${r.stats.new_usage?.toFixed(4)} ${r.stats.unit}</span>
              <span class="stat">🕐 through ${r.stats.newest_time || "—"}</span>
            </div>`
            : "";

        return `
          <div class="result-card ${r.type}">
            <div class="result-title">
              ${icons[r.type]}
              ${this._esc(r.filename)}
              <span style="font-weight:400;color:var(--secondary-text-color)">
                (${r.serviceType})
              </span>
            </div>
            <div class="result-detail">
              ${this._esc(r.message)}
              ${statsHtml}
            </div>
            <div class="result-time">${r.timestamp}</div>
          </div>`;
      })
      .join("");
  }

  _esc(str) {
    return String(str ?? "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }
}

customElements.define("green-button-energy-panel", RgeGreenButtonPanel);