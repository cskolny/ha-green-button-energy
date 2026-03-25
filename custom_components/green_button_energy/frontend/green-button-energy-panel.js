/**
 * Green Button Energy Import Panel
 *
 * A custom Home Assistant sidebar panel that provides drag-and-drop
 * file import for Avangrid Green Button CSV/XML exports (hourly usage)
 * and monthly billing CSV exports (cost data).
 *
 * Architecture:
 *   - Runs entirely in the browser as a native Web Component
 *   - Validates file size client-side before reading (matches 10 MB backend limit)
 *   - Reads the dropped file as UTF-8 text via FileReader
 *   - Sends content over the existing HA WebSocket connection
 *     using hass.connection.sendMessagePromise (no extra auth needed)
 *   - Backend WebSocket handler parses the file in memory and
 *     updates the sensors directly — no filesystem access required
 *
 * Two WebSocket message types:
 *   green_button_energy/import_file    — hourly usage CSV/XML
 *   green_button_energy/import_billing — monthly billing CSV
 */

// @version 1.6.0

// Maximum file size — must match _MAX_FILE_BYTES in __init__.py
const _MAX_FILE_MB = 10;
const _MAX_FILE_BYTES = _MAX_FILE_MB * 1024 * 1024;

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
          max-width: 900px;
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

        /* ── Section headers ── */
        .section-header {
          font-size: 0.85rem;
          font-weight: 500;
          color: var(--secondary-text-color, #727272);
          text-transform: uppercase;
          letter-spacing: 0.06em;
          margin: 0 0 10px;
          display: flex;
          align-items: center;
          gap: 6px;
        }

        .section-hint {
          font-size: 0.8rem;
          color: var(--secondary-text-color, #727272);
          margin: -6px 0 14px;
        }

        .section-hint a {
          color: var(--primary-color, #03a9f4);
          text-decoration: none;
        }

        /* ── Drop zones ── */
        .zones {
          display: grid;
          grid-template-columns: 1fr 1fr;
          gap: 16px;
          margin-bottom: 28px;
        }

        @media (max-width: 600px) {
          .zones { grid-template-columns: 1fr; }
        }

        .drop-zone {
          border: 2px dashed var(--divider-color, #e0e0e0);
          border-radius: 12px;
          padding: 28px 20px;
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
          font-size: 2.2rem;
          margin-bottom: 8px;
          display: block;
        }

        .drop-zone .label {
          font-size: 1rem;
          font-weight: 500;
          color: var(--primary-text-color, #212121);
          margin-bottom: 4px;
        }

        .drop-zone .hint {
          font-size: 0.78rem;
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

        /* ── Divider ── */
        .section-divider {
          border: none;
          border-top: 1px solid var(--divider-color, #e0e0e0);
          margin: 8px 0 24px;
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

        .result-badge {
          font-size: 0.7rem;
          font-weight: 400;
          background: var(--primary-background-color, #f5f5f5);
          border-radius: 4px;
          padding: 1px 6px;
          color: var(--secondary-text-color, #727272);
          margin-left: 2px;
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

        /* ── Info box ── */
        .info-box {
          background: color-mix(in srgb, var(--primary-color, #03a9f4) 8%, transparent);
          border: 1px solid color-mix(in srgb, var(--primary-color, #03a9f4) 30%, transparent);
          border-radius: 8px;
          padding: 12px 14px;
          font-size: 0.82rem;
          color: var(--primary-text-color, #212121);
          margin-bottom: 20px;
          line-height: 1.5;
        }

        .info-box strong {
          color: var(--primary-color, #03a9f4);
        }
      </style>

      <div class="page">
        <h1>
          <ha-icon icon="mdi:lightning-bolt-circle"></ha-icon>
          Green Button Energy Import
        </h1>
        <p class="subtitle">
          Download your usage and billing data from <strong>your utility website → My Energy Use → Download Data</strong>,
          then drop the files below.
        </p>

        <!-- ── Hourly Usage Section ── -->
        <div class="section-header">⚡ Hourly Usage Data</div>
        <p class="section-hint">
          CSV or XML — imports hourly kWh/therm readings into the Energy Dashboard history.
        </p>

        <div class="zones">
          <div class="drop-zone" id="zone-electric" data-service-type="electric" data-import-type="usage">
            <input type="file" accept=".csv,.xml" id="file-electric" data-service-type="electric" data-import-type="usage" />
            <span class="icon">⚡</span>
            <div class="label">Electric Usage</div>
            <div class="hint">CSV or XML · hourly kWh</div>
          </div>

          <div class="drop-zone" id="zone-gas" data-service-type="gas" data-import-type="usage">
            <input type="file" accept=".csv,.xml" id="file-gas" data-service-type="gas" data-import-type="usage" />
            <span class="icon">🔥</span>
            <div class="label">Gas Usage</div>
            <div class="hint">CSV or XML · hourly therms</div>
          </div>
        </div>

        <hr class="section-divider" />

        <!-- ── Monthly Billing Section ── -->
        <div class="section-header">💰 Monthly Billing Data</div>
        <p class="section-hint">
          Billing CSV only — imports monthly dollar costs so the Energy Dashboard can show your actual spend.
          After importing, go to <strong>Settings → Energy</strong> and set the cost sensor for each commodity.
        </p>

        <div class="info-box">
          <strong>How to use billing data in the Energy Dashboard:</strong>
          After importing, go to <strong>Settings → Energy → Electricity grid → Edit</strong> and
          change "Use a static price" to "Use an entity tracking the total cost" →
          select <strong>Avangrid Electric Cost</strong>.
          Do the same for Gas. The Energy Dashboard will then show your actual monthly bills
          spread across the billing period.
        </div>

        <div class="zones">
          <div class="drop-zone" id="zone-electric-billing" data-service-type="electric" data-import-type="billing">
            <input type="file" accept=".csv" id="file-electric-billing" data-service-type="electric" data-import-type="billing" />
            <span class="icon">🧾</span>
            <div class="label">Electric Billing</div>
            <div class="hint">CSV only · monthly $ costs</div>
          </div>

          <div class="drop-zone" id="zone-gas-billing" data-service-type="gas" data-import-type="billing">
            <input type="file" accept=".csv" id="file-gas-billing" data-service-type="gas" data-import-type="billing" />
            <span class="icon">🧾</span>
            <div class="label">Gas Billing</div>
            <div class="hint">CSV only · monthly $ costs</div>
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

    const zones = [
      { zoneId: "zone-electric",         fileId: "file-electric",         serviceType: "electric", importType: "usage"    },
      { zoneId: "zone-gas",              fileId: "file-gas",              serviceType: "gas",      importType: "usage"    },
      { zoneId: "zone-electric-billing", fileId: "file-electric-billing", serviceType: "electric", importType: "billing"  },
      { zoneId: "zone-gas-billing",      fileId: "file-gas-billing",      serviceType: "gas",      importType: "billing"  },
    ];

    zones.forEach(({ zoneId, fileId, serviceType, importType }) => {
      const zone = root.getElementById(zoneId);
      const input = root.getElementById(fileId);

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
        if (file) this._handleFile(file, serviceType, importType, zone);
      });

      input.addEventListener("change", (e) => {
        const file = e.target.files?.[0];
        if (file) this._handleFile(file, serviceType, importType, zone);
        input.value = "";
      });
    });

    root.getElementById("clear-btn").addEventListener("click", () => {
      this._results = [];
      this._refreshResults();
    });
  }

  async _handleFile(file, serviceType, importType, zone) {
    const ext = file.name.split(".").pop().toLowerCase();

    // Billing zones only accept CSV
    if (importType === "billing" && ext !== "csv") {
      this._addResult({
        type: "error",
        filename: file.name,
        serviceType,
        importType,
        message: `Billing imports only support .csv files. Got ".${ext}".`,
      });
      return;
    }

    // Usage zones accept CSV and XML
    if (importType === "usage" && !["csv", "xml"].includes(ext)) {
      this._addResult({
        type: "error",
        filename: file.name,
        serviceType,
        importType,
        message: `Unsupported file type ".${ext}". Please use .csv or .xml.`,
      });
      return;
    }

    if (file.size > _MAX_FILE_BYTES) {
      this._addResult({
        type: "error",
        filename: file.name,
        serviceType,
        importType,
        message: `File is too large (${(file.size / 1024 / 1024).toFixed(1)} MB). Maximum is ${_MAX_FILE_MB} MB.`,
      });
      return;
    }

    // Show processing state
    zone.classList.add("processing");
    const hintEl = zone.querySelector(".hint");
    const originalHint = hintEl.textContent;
    hintEl.textContent = "Importing…";
    const spinner = document.createElement("div");
    spinner.className = "spinner";
    zone.appendChild(spinner);

    try {
      const content = await this._readFileAsText(file);

      if (!this._hass?.connection) {
        throw new Error("Lost connection to Home Assistant. Please refresh and try again.");
      }

      let response;

      if (importType === "usage") {
        response = await this._hass.connection.sendMessagePromise({
          type: "green_button_energy/import_file",
          filename: file.name,
          content: content,
          service_type: serviceType,
        });
        this._handleUsageResponse(response, file.name, serviceType);
      } else {
        response = await this._hass.connection.sendMessagePromise({
          type: "green_button_energy/import_billing",
          filename: file.name,
          content: content,
          service_type: serviceType,
        });
        this._handleBillingResponse(response, file.name, serviceType);
      }

    } catch (err) {
      const msg = err?.message || String(err) || "Unknown connection error.";
      this._addResult({
        type: "error",
        filename: file.name,
        serviceType,
        importType,
        message: `Error: ${msg}`,
      });
    } finally {
      zone.classList.remove("processing");
      hintEl.textContent = originalHint;
      spinner.remove();
    }
  }

  _handleUsageResponse(response, filename, serviceType) {
    if (response.success) {
      if (response.rows_written === 0) {
        this._addResult({
          type: "warning",
          filename,
          serviceType,
          importType: "usage",
          message: "No new data — file is already fully imported.",
          stats: response,
        });
      } else {
        this._addResult({
          type: "success",
          filename,
          serviceType,
          importType: "usage",
          message: "Import successful!",
          stats: response,
        });
      }
    } else {
      this._addResult({
        type: "error",
        filename,
        serviceType,
        importType: "usage",
        message: response.error || "Import failed.",
        stats: response,
      });
    }
  }

  _handleBillingResponse(response, filename, serviceType) {
    if (response.success) {
      if (response.rows_written === 0) {
        this._addResult({
          type: "warning",
          filename,
          serviceType,
          importType: "billing",
          message: "No new billing data — all cycles already imported.",
          stats: response,
        });
      } else {
        this._addResult({
          type: "success",
          filename,
          serviceType,
          importType: "billing",
          message: "Billing import successful!",
          stats: response,
        });
      }
    } else {
      this._addResult({
        type: "error",
        filename,
        serviceType,
        importType: "billing",
        message: response.error || "Billing import failed.",
        stats: response,
      });
    }
  }

  _readFileAsText(file) {
    return new Promise((resolve, reject) => {
      const reader = new FileReader();
      reader.onload = (e) => resolve(e.target.result);
      reader.onerror = () => reject(new Error("Failed to read file — it may be corrupt or unreadable."));
      reader.readAsText(file, "UTF-8");
    });
  }

  _addResult(result) {
    result.timestamp = new Date().toLocaleTimeString();
    this._results.unshift(result);
    if (this._results.length > 30) this._results.pop();
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
        let statsHtml = "";

        if (r.importType === "usage" && r.stats && r.stats.rows_written > 0) {
          statsHtml = `
            <div style="margin-top:6px">
              <span class="stat">📥 ${r.stats.rows_written} rows written</span>
              <span class="stat">📊 ${r.stats.new_usage?.toFixed(4)} ${r.stats.unit}</span>
              <span class="stat">🕐 through ${r.stats.newest_time || "—"}</span>
            </div>`;
        } else if (r.importType === "billing" && r.stats && r.stats.rows_written > 0) {
          statsHtml = `
            <div style="margin-top:6px">
              <span class="stat">📅 ${r.stats.cycles_imported} billing cycles</span>
              <span class="stat">💰 $${r.stats.new_cost?.toFixed(2)}</span>
              <span class="stat">🕐 through ${r.stats.newest_time || "—"}</span>
            </div>`;
        }

        const badge = r.importType === "billing" ? "billing" : r.serviceType;

        return `
          <div class="result-card ${r.type}">
            <div class="result-title">
              ${icons[r.type]}
              ${this._esc(r.filename)}
              <span class="result-badge">${badge}</span>
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

customElements.define("green-button-energy-panel", GreenButtonEnergyPanel);
