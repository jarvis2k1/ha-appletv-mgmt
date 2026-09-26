/**
 * appletv-mgmt-history-card
 *
 * A small custom Lovelace card showing today's Apple TV app usage:
 * - per-app totals (bar chart with proportional widths)
 * - chronological session list (timeline with start/end + duration)
 *
 * Reads from the sensor created by the appletv_mgmt integration:
 *   sensor.<profile>_today_history  (attributes: apps[], events[])
 *
 * Single file, no build step. Uses Lit via CDN (already loaded by HA).
 *
 * Usage in a dashboard:
 *
 *   type: custom:appletv-mgmt-history-card
 *   entity: sensor.living_room_today_history
 *   show_events: true     # optional, default true
 *   show_apps: true       # optional, default true
 *   max_events: 30        # optional, default 30
 *
 * Resource registration (one-time, via UI: Settings -> Dashboards -> Resources):
 *   URL: /local/community/appletv-mgmt-history-card/appletv-mgmt-history-card.js
 *   Resource type: JavaScript module
 */

// Import Lit as an ES module — see appletv-mgmt-control-card.js for
// rationale. The old prototype-grab pattern silently fails when HA's
// internal classes aren't registered at module-load time.
import {
  LitElement,
  html,
  css,
} from 'https://unpkg.com/lit-element@4.1.1/lit-element.js?module';

class AppleTVMgmtHistoryCard extends LitElement {
  static get properties() {
    return {
      hass: { type: Object },
      config: { type: Object },
    };
  }

  setConfig(config) {
    if (!config || !config.entity) {
      throw new Error('You must specify an "entity" (a sensor.*_today_history).');
    }
    this.config = {
      show_apps: true,
      show_events: true,
      max_events: 30,
      ...config,
    };
  }

  getCardSize() {
    return 6;
  }

  _formatTime(iso) {
    if (!iso) return '—';
    const d = new Date(iso);
    return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
  }

  _formatDuration(minutes) {
    if (minutes < 1) return `${Math.round(minutes * 60)}s`;
    if (minutes < 60) return `${minutes.toFixed(1)} min`;
    const h = Math.floor(minutes / 60);
    const m = Math.round(minutes - h * 60);
    return `${h}h ${m}m`;
  }

  _renderApps(apps) {
    if (!apps || apps.length === 0) {
      return html`<div class="empty">No usage today yet.</div>`;
    }
    const max = apps.reduce((m, a) => Math.max(m, a.total_minutes), 1);
    return html`
      <div class="apps">
        ${apps.map(
          (app) => html`
            <div class="app">
              <div class="app-row">
                <span class="app-name">${app.display_name}</span>
                <span class="app-total">${this._formatDuration(app.total_minutes)}</span>
              </div>
              <div class="bar-bg">
                <div class="bar" style="width: ${(app.total_minutes / max) * 100}%"></div>
              </div>
              <div class="app-sub">
                ${app.sessions} ${app.sessions === 1 ? 'session' : 'sessions'} ·
                <span class="bundle">${app.bundle_id}</span>
              </div>
            </div>
          `,
        )}
      </div>
    `;
  }

  _renderEvents(events) {
    if (!events || events.length === 0) {
      return html`<div class="empty">No sessions recorded yet today.</div>`;
    }
    const trimmed = events.slice(-this.config.max_events).reverse(); // newest first
    return html`
      <div class="events">
        ${trimmed.map(
          (e) => html`
            <div class="event ${e.open ? 'event-open' : ''}">
              <div class="event-time">
                ${this._formatTime(e.started_at)} →
                ${e.open ? html`<span class="now">now</span>` : this._formatTime(e.ended_at)}
              </div>
              <div class="event-app">${e.display_name}</div>
              <div class="event-duration">${this._formatDuration(e.duration_minutes)}</div>
            </div>
          `,
        )}
      </div>
    `;
  }

  render() {
    if (!this.hass || !this.config) {
      return html``;
    }
    const stateObj = this.hass.states[this.config.entity];
    if (!stateObj) {
      return html`<ha-card><div class="empty">Entity ${this.config.entity} not found.</div></ha-card>`;
    }
    const attrs = stateObj.attributes || {};
    const apps = attrs.apps || [];
    const events = attrs.events || [];
    const distinct = parseInt(stateObj.state, 10) || 0;

    return html`
      <ha-card>
        <div class="header">
          <div class="title">Apple TV — today</div>
          <div class="subtitle">
            ${distinct} ${distinct === 1 ? 'app' : 'apps'} ·
            ${events.length} ${events.length === 1 ? 'session' : 'sessions'}
          </div>
        </div>
        ${this.config.show_apps ? this._renderApps(apps) : ''}
        ${this.config.show_apps && this.config.show_events ? html`<div class="divider"></div>` : ''}
        ${this.config.show_events ? this._renderEvents(events) : ''}
      </ha-card>
    `;
  }

  static get styles() {
    return css`
      ha-card {
        padding: 16px;
      }
      .header {
        display: flex;
        justify-content: space-between;
        align-items: baseline;
        margin-bottom: 12px;
      }
      .title {
        font-size: 1.1em;
        font-weight: 500;
      }
      .subtitle {
        color: var(--secondary-text-color);
        font-size: 0.85em;
      }
      .empty {
        color: var(--secondary-text-color);
        padding: 12px 0;
        text-align: center;
        font-style: italic;
      }

      .apps {
        display: flex;
        flex-direction: column;
        gap: 10px;
      }
      .app-row {
        display: flex;
        justify-content: space-between;
        align-items: baseline;
      }
      .app-name {
        font-weight: 500;
      }
      .app-total {
        font-variant-numeric: tabular-nums;
        color: var(--primary-text-color);
      }
      .bar-bg {
        height: 6px;
        background: var(--divider-color);
        border-radius: 3px;
        overflow: hidden;
        margin-top: 4px;
      }
      .bar {
        height: 100%;
        background: var(--primary-color);
        border-radius: 3px;
        transition: width 0.3s ease;
      }
      .app-sub {
        color: var(--secondary-text-color);
        font-size: 0.8em;
        margin-top: 2px;
      }
      .bundle {
        font-family: var(--code-font-family, monospace);
        font-size: 0.9em;
      }

      .divider {
        height: 1px;
        background: var(--divider-color);
        margin: 16px 0;
      }

      .events {
        display: flex;
        flex-direction: column;
        gap: 4px;
        max-height: 320px;
        overflow-y: auto;
      }
      .event {
        display: grid;
        grid-template-columns: 110px 1fr auto;
        gap: 8px;
        align-items: center;
        padding: 4px 0;
        font-size: 0.9em;
      }
      .event + .event {
        border-top: 1px dashed var(--divider-color);
      }
      .event-open {
        background: rgba(var(--rgb-primary-color, 33, 150, 243), 0.08);
        border-radius: 4px;
        padding: 4px 6px;
      }
      .event-time {
        color: var(--secondary-text-color);
        font-variant-numeric: tabular-nums;
      }
      .now {
        color: var(--primary-color);
        font-weight: 500;
      }
      .event-app {
        font-weight: 500;
      }
      .event-duration {
        color: var(--secondary-text-color);
        font-variant-numeric: tabular-nums;
      }
    `;
  }
}

customElements.define('appletv-mgmt-history-card', AppleTVMgmtHistoryCard);

window.customCards = window.customCards || [];
window.customCards.push({
  type: 'appletv-mgmt-history-card',
  name: 'Apple TV Mgmt — History',
  description: "Today's per-app usage and session timeline for the appletv_mgmt integration.",
});

console.info('%c appletv-mgmt-history-card %c 0.1.0 ', 'color: #fff; background: #4caf50; padding: 2px 4px; border-radius: 3px;', 'color: #4caf50;');
