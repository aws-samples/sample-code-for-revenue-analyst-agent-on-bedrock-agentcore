// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * Analyst SPA -- login screen + chat UI. Vanilla JS, no framework: a chat
 * surface with a login form is not complex enough to justify a
 * React/Vue build for this sample.
 *
 * IMPORTANT design note on the record_recommendation "confirm" flow:
 * confirmation is entirely CONVERSATIONAL, not a
 * separate structured API call. The agent's system prompt requires it to
 * (1) call propose_recommendation and ask the analyst to confirm in plain
 * text, then (2) only call record_recommendation after the analyst
 * explicitly says yes in a LATER chat message. There is no direct
 * "confirm this recommendationId" endpoint for the SPA to call -- the gate
 * is server-side (see recommendation_client.py) but the CONFIRMATION
 * SIGNAL itself is just another normal chat message. This UI reflects
 * that: it never calls any special confirm API. It only offers a "Yes,
 * confirm" QUICK-REPLY button (heuristically shown when the agent's own
 * response looks like it's asking for confirmation) that sends that exact
 * text as the next ordinary chat turn -- same pipeline as any other
 * message, no backend special-casing.
 */
import DOMPurify from "dompurify";
import { marked } from "marked";
import { login, isTokenExpired } from "./auth.js";
import {
  initClient,
  invokeAgent,
  resetSession,
  listSessions,
  getHistory,
  deleteSession,
  getSessionId,
  setSessionId,
} from "./agent-client.js";

const app = document.getElementById("app");

let state = {
  idToken: null,
  claims: null,
  history: [], // { role: "user" | "agent", text: string, isError?: boolean }
  sessions: [], // past conversations for the signed-in user (sidebar)
};

function render() {
  app.innerHTML = "";
  if (!state.idToken || isTokenExpired(state.claims)) {
    app.appendChild(renderLogin());
  } else {
    app.appendChild(renderChat());
  }
}

function renderLogin() {
  const wrap = document.createElement("div");
  wrap.className = "login-screen";
  // nosemgrep: insecure-innerhtml,insecure-document-method,html-in-template-string -- static markup, no user-controlled interpolation
  wrap.innerHTML = `
    <div class="login-card">
      <div class="brand"><div class="brand-logo">R</div></div>
      <h1>Revenue Analyst Agent</h1>
      <p class="subtitle">Sign in with your analyst account.</p>
      <p class="error-text" id="login-error" hidden></p>
      <label for="username">Email</label>
      <input id="username" type="email" autocomplete="username" />
      <label for="password">Password</label>
      <input id="password" type="password" autocomplete="current-password" />
      <button id="login-btn">Sign in</button>
    </div>
  `;

  const btn = wrap.querySelector("#login-btn");
  const errorEl = wrap.querySelector("#login-error");

  const submit = async () => {
    const username = wrap.querySelector("#username").value.trim();
    const password = wrap.querySelector("#password").value;
    if (!username || !password) return;

    btn.disabled = true;
    btn.textContent = "Signing in...";
    errorEl.hidden = true;

    try {
      const { idToken, claims } = await login(username, password);
      // No AWS credential exchange needed anymore -- the Runtime's
      // CUSTOM_JWT authorizer validates the raw ID token directly (see
      // agent-client.js's module docstring). initClient() just starts a
      // fresh runtime session id.
      initClient();

      state.idToken = idToken;
      state.claims = claims;
      state.history = [];
      state.sessions = [];
      render();
    } catch (err) {
      errorEl.textContent = err.message;
      errorEl.hidden = false;
      btn.disabled = false;
      btn.textContent = "Sign in";
    }
  };

  btn.addEventListener("click", submit);
  wrap.querySelector("#password").addEventListener("keydown", (e) => {
    if (e.key === "Enter") submit();
  });

  return wrap;
}

// Static chat shell markup. It contains no interpolated values: anything
// user-derived is filled in with textContent after cloning (see renderChat).
const CHAT_SHELL_TEMPLATE = document.createElement("template");
CHAT_SHELL_TEMPLATE.innerHTML = `
    <aside class="sidebar">
      <button id="new-chat-btn" class="new-chat-btn">+ New chat</button>
      <div class="sidebar-label">Your conversations</div>
      <div class="session-list" id="session-list">
        <div class="session-empty">Loading…</div>
      </div>
    </aside>
    <div class="chat-pane">
      <div class="chat-header">
        <div class="brand">
          <div class="brand-logo">R</div>
          <span class="brand-title">Revenue Analyst Agent</span>
        </div>
        <div class="spacer"></div>
        <div class="who"><span id="who-email"></span> &middot; <span class="role" id="who-role"></span></div>
        <button id="signout-btn">Sign out</button>
      </div>
      <div class="chat-body" id="chat-body"></div>
      <div class="typing" id="typing-indicator" hidden>
        <span>Agent is thinking</span>
        <span class="dots"><span></span><span></span><span></span></span>
      </div>
      <div class="chat-input">
        <textarea id="chat-input" rows="1" placeholder="Ask about revenue, occupancy, or booking performance..."></textarea>
        <button id="send-btn">Send</button>
      </div>
    </div>
`;

function renderChat() {
  const wrap = document.createElement("div");
  wrap.className = "app-shell";

  const email = state.claims?.email || state.claims?.["cognito:username"] || "analyst";
  const groups = (state.claims?.["cognito:groups"] || []).join(", ") || "no role";

  // The chat shell markup is a constant (CHAT_SHELL_TEMPLATE). The only
  // user-derived values, the analyst's email and role, are set below with
  // textContent, so no user data is ever parsed as HTML.
  wrap.appendChild(CHAT_SHELL_TEMPLATE.content.cloneNode(true));
  wrap.querySelector("#who-email").textContent = email;
  wrap.querySelector("#who-role").textContent = groups;

  wrap.querySelector("#signout-btn").addEventListener("click", () => {
    state = { idToken: null, claims: null, history: [], sessions: [] };
    render();
  });

  const bodyEl = wrap.querySelector("#chat-body");
  const inputEl = wrap.querySelector("#chat-input");
  const sendBtn = wrap.querySelector("#send-btn");
  const typingEl = wrap.querySelector("#typing-indicator");
  const sessionListEl = wrap.querySelector("#session-list");
  const newChatBtn = wrap.querySelector("#new-chat-btn");

  function renderHistory() {
    bodyEl.innerHTML = "";
    for (const msg of state.history) {
      bodyEl.appendChild(renderMessage(msg, sendMessage));
    }
    bodyEl.scrollTop = bodyEl.scrollHeight;
  }

  function renderSessions() {
    sessionListEl.innerHTML = "";
    // Only conversations that actually have a first message (belt-and-
    // suspenders; the backend already filters empties).
    const visible = state.sessions.filter(
      (s) => s.preview && s.preview.trim() && s.preview !== "(no messages)"
    );
    if (!visible.length) {
      const empty = document.createElement("div");
      empty.className = "session-empty";
      empty.textContent = "No past conversations yet.";
      sessionListEl.appendChild(empty);
      return;
    }
    const current = getSessionId();
    for (const s of visible) {
      const row = document.createElement("div");
      row.className = "session-row" + (s.sessionId === current ? " active" : "");

      const item = document.createElement("button");
      item.className = "session-item";
      item.textContent = s.preview;
      item.title = s.preview;
      item.addEventListener("click", () => openSession(s.sessionId));

      const del = document.createElement("button");
      del.className = "session-delete";
      del.textContent = "\u00d7"; // ×
      del.title = "Delete conversation";
      del.setAttribute("aria-label", "Delete conversation");
      del.addEventListener("click", (e) => {
        e.stopPropagation();
        removeSession(s.sessionId);
      });

      row.appendChild(item);
      row.appendChild(del);
      sessionListEl.appendChild(row);
    }
  }

  async function removeSession(id) {
    if (isTokenExpired(state.claims)) {
      state.idToken = null;
      render();
      return;
    }
    // optimistic: drop it from the list immediately, then confirm server-side
    state.sessions = state.sessions.filter((s) => s.sessionId !== id);
    // if we deleted the conversation currently open, start a fresh chat
    if (getSessionId() === id) {
      resetSession();
      state.history = [];
      renderHistory();
    }
    renderSessions();
    try {
      await deleteSession(id, state.idToken);
    } catch {
      // ignore -- the next refresh will reconcile if the delete didn't take
    }
    refreshSessions();
  }

  async function refreshSessions() {
    try {
      state.sessions = await listSessions(state.idToken);
    } catch {
      state.sessions = [];
    }
    renderSessions();
  }

  async function openSession(id) {
    if (isTokenExpired(state.claims)) {
      state.idToken = null;
      render();
      return;
    }
    setSessionId(id);
    typingEl.hidden = false;
    try {
      const messages = await getHistory(id, state.idToken);
      state.history = messages.map((m) => ({ role: m.role, text: m.text }));
    } catch (err) {
      state.history = [
        { role: "agent", text: `Could not load that conversation: ${err.message}`, isError: true },
      ];
    } finally {
      typingEl.hidden = true;
      renderHistory();
      renderSessions();
    }
  }

  function startNewChat() {
    resetSession();
    state.history = [];
    renderHistory();
    renderSessions();
    inputEl.focus();
  }

  newChatBtn.addEventListener("click", startNewChat);

  async function sendMessage(text) {
    if (!text || !text.trim()) return;
    if (isTokenExpired(state.claims)) {
      state.idToken = null;
      render();
      return;
    }

    const isFirstTurn = state.history.length === 0;

    state.history.push({ role: "user", text });
    renderHistory();
    inputEl.value = "";
    sendBtn.disabled = true;
    typingEl.hidden = false;

    try {
      const result = await invokeAgent(text, state.idToken);
      const isError = result.status !== "success";
      const responseText = result.response || result.error || "The agent returned no response.";
      state.history.push({
        role: "agent",
        text: responseText,
        isError,
        modelLabel: result.model_label || null,
        routerLabel: result.router_model_label || null,
        complexity: result.routing_complexity || null,
        dataSources: Array.isArray(result.data_sources) ? result.data_sources : [],
      });
    } catch (err) {
      state.history.push({
        role: "agent",
        text: `Request failed: ${err.message}`,
        isError: true,
      });
    } finally {
      sendBtn.disabled = false;
      typingEl.hidden = true;
      renderHistory();
      // After the first turn of a brand-new chat, the conversation now
      // exists in memory -- refresh the sidebar so it shows up.
      if (isFirstTurn) {
        refreshSessions();
      }
    }
  }

  sendBtn.addEventListener("click", () => sendMessage(inputEl.value));
  inputEl.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      sendMessage(inputEl.value);
    }
  });

  renderHistory();
  renderSessions();
  refreshSessions();
  return wrap;
}

/**
 * Heuristic: does this agent response look like it's asking the analyst
 * to confirm a proposed recommendation? Looks for the shape the system
 * prompt actually produces (see agent/agent.py's SYSTEM_PROMPT rule 9 --
 * propose_recommendation's result always surfaces a recommendationId the
 * model is instructed to present alongside an explicit ask for
 * confirmation), not a guess at arbitrary phrasing.
 */
function looksLikeConfirmationRequest(text) {
  return /confirm/i.test(text) && /(recommend|propos)/i.test(text);
}

function renderMessage(msg, onQuickReply) {
  const el = document.createElement("div");
  el.className = `message ${msg.role}${msg.isError ? " error" : ""}`;

  if (msg.role === "user") {
    el.textContent = msg.text;
  } else {
    // Agent output is markdown. DOMPurify sanitizes the rendered HTML and
    // returns DOM nodes (RETURN_DOM_FRAGMENT), which are appended directly,
    // so the sanitized result is never re-parsed from a string. Inline
    // `style` attributes are stripped too: the CSP has no 'unsafe-inline'
    // for styles, so they would be blocked anyway.
    const fragment = DOMPurify.sanitize(marked.parse(msg.text), {
      RETURN_DOM_FRAGMENT: true,
      FORBID_ATTR: ["style"],
    });
    el.appendChild(fragment);

    // Demo transparency: show which model tier produced this answer, and
    // note that Haiku did the routing classification.
    if (!msg.isError && msg.modelLabel) {
      const badgeRow = document.createElement("div");
      badgeRow.className = "badge-row";

      const badge = document.createElement("span");
      badge.className = "model-badge";
      let label = msg.modelLabel;
      if (msg.routerLabel) {
        label += ` · routed by ${msg.routerLabel}`;
      }
      badge.textContent = label;
      badgeRow.appendChild(badge);

      // Data-source badge(s): where this answer's data came from
      // (Athena event lake vs the reporting API). Only shown when the
      // turn actually fetched data.
      if (Array.isArray(msg.dataSources) && msg.dataSources.length) {
        const src = document.createElement("span");
        src.className = "source-badge";
        src.textContent = "Data: " + msg.dataSources.join(" + ");
        badgeRow.appendChild(src);
      }

      // Demo disclaimer travels with the response metadata, not the header.
      const demo = document.createElement("span");
      demo.className = "demo-badge";
      demo.textContent = "For demo purposes only";
      badgeRow.appendChild(demo);

      el.appendChild(badgeRow);
    }

    if (!msg.isError && looksLikeConfirmationRequest(msg.text)) {
      const btn = document.createElement("button");
      btn.textContent = "Yes, confirm";
      btn.className = "quick-reply";
      // Sends a plain, ordinary chat message -- the SAME pipeline as
      // anything the analyst types by hand. No special API, no backend
      // special-casing (see module docstring).
      btn.addEventListener("click", () => onQuickReply("Yes, I confirm."));
      el.appendChild(document.createElement("br"));
      el.appendChild(btn);
    }
  }

  return el;
}

render();
