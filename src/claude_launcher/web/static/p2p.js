/* The page's control socket over a WebRTC DataChannel (claunch-mhzt4).

   Through the relay every read and keystroke crosses the Cloudflare edge:
   454-761ms per round trip measured from the user's network, where a
   DataChannel straight to the daemon host answered in 11-13ms. This file is
   the page's half of that channel; daemon/p2p.py is the other.

   - ClaunchP2P.encode / Reassembler: the framing both ends agree on. Every
     DataChannel message is binary: one header byte (bit 0 = the control
     frame is binary, bit 1 = more fragments follow) and at most 16 KiB.
   - ClaunchP2P.DirectSocket: the part of WebSocket app.js uses, over one
     DataChannel, so the control socket code does not care which road it
     is on.
   - ClaunchP2P.Link: one negotiation. The offer goes on the relay control
     socket as soon as gathering completes or GATHER_CAP_MS pass, whichever
     is first; candidates found later follow as p2p_ice frames. The answer
     carries a single-use nonce, which is the first thing sent on the
     channel. The link is ready when the daemon's `init` comes back over it.

   Nothing here decides WHEN to try or what to do on failure: app.js does
   (p2pAttempt, controlAdopt), and the relay socket stays the fallback. */
(function () {
  "use strict";

  const FRAGMENT = 16 * 1024;
  const FLAG_BINARY = 0x01;
  const FLAG_MORE = 0x02;
  const GATHER_CAP_MS = 1500;
  const READY_TIMEOUT_MS = 15000;
  const LABEL = "claunch-control";

  function bytesOf(data) {
    if (typeof data === "string") return new TextEncoder().encode(data);
    if (data instanceof ArrayBuffer) return new Uint8Array(data);
    if (ArrayBuffer.isView(data)) return new Uint8Array(data.buffer, data.byteOffset, data.byteLength);
    throw new TypeError("cannot send " + typeof data);
  }

  function encode(data) {
    const flag = typeof data === "string" ? 0 : FLAG_BINARY;
    const body = bytesOf(data);
    if (!body.length) return [Uint8Array.of(flag)];
    const out = [];
    for (let at = 0; at < body.length; at += FRAGMENT) {
      const chunk = body.subarray(at, at + FRAGMENT);
      const msg = new Uint8Array(chunk.length + 1);
      msg[0] = flag | (at + FRAGMENT < body.length ? FLAG_MORE : 0);
      msg.set(chunk, 1);
      out.push(msg);
    }
    return out;
  }

  class Reassembler {
    constructor() { this.parts = []; this.size = 0; this.binary = null; }
    /* {binary, data} when `message` completes a frame, else null. */
    feed(message) {
      if (typeof message === "string") throw new Error("DataChannel frames are binary");
      const bytes = bytesOf(message);
      if (!bytes.length) throw new Error("empty DataChannel message");
      const binary = !!(bytes[0] & FLAG_BINARY);
      if (this.binary !== null && binary !== this.binary) throw new Error("fragment kind changed");
      this.binary = binary;
      this.parts.push(bytes.subarray(1));
      this.size += bytes.length - 1;
      if (bytes[0] & FLAG_MORE) return null;
      const whole = new Uint8Array(this.size);
      let at = 0;
      for (const p of this.parts) { whole.set(p, at); at += p.length; }
      this.parts = []; this.size = 0; this.binary = null;
      return binary
        ? { binary: true, data: whole.buffer }
        : { binary: false, data: new TextDecoder().decode(whole) };
    }
  }

  class DirectSocket {
    constructor(dc) {
      this.dc = dc;
      this.transport = "p2p";
      this.binaryType = "arraybuffer";
      this.readyState = DirectSocket.CONNECTING;
      this.onopen = null;
      this.onmessage = null;
      this.onclose = null;
      this.onerror = null;
      this.asm = new Reassembler();
      dc.binaryType = "arraybuffer";
      dc.onmessage = (ev) => this._message(ev.data);
      dc.onclose = () => this._closed(1006, "datachannel closed");
      dc.onerror = () => this._closed(1006, "datachannel error");
    }
    get bufferedAmount() { return this.dc.bufferedAmount || 0; }
    /* Throws when not open, as a WebSocket in CONNECTING does: controlRead
       turns that into its HTTP fallback, and nothing is sent into a channel
       that will not deliver it. */
    send(data) {
      if (this.readyState !== DirectSocket.OPEN) throw new Error("direct socket is not open");
      for (const part of encode(data)) this.dc.send(part);
    }
    close() {
      if (this.readyState >= DirectSocket.CLOSING) return;
      this.readyState = DirectSocket.CLOSING;
      try { this.dc.close(); } catch { /* already closed */ }
      this._closed(1000, "closed");
    }
    _open() {
      if (this.readyState !== DirectSocket.CONNECTING) return;
      this.readyState = DirectSocket.OPEN;
      if (this.onopen) this.onopen({ target: this });
    }
    _message(raw) {
      let frame;
      try { frame = this.asm.feed(raw); } catch (err) {
        this._closed(1006, String(err && err.message || err));
        try { this.dc.close(); } catch { /* ignore */ }
        return;
      }
      if (!frame || this.readyState !== DirectSocket.OPEN || !this.onmessage) return;
      this.onmessage({ data: frame.data, target: this });
    }
    _closed(code, reason) {
      if (this.readyState === DirectSocket.CLOSED) return;
      this.readyState = DirectSocket.CLOSED;
      if (this.onclose) this.onclose({ code, reason, target: this });
    }
  }
  DirectSocket.CONNECTING = 0;
  DirectSocket.OPEN = 1;
  DirectSocket.CLOSING = 2;
  DirectSocket.CLOSED = 3;

  function sdpCandidates(sdp) {
    const out = new Set();
    for (const line of String(sdp || "").split(/\r?\n/)) {
      if (line.startsWith("a=candidate:")) out.add(line.slice(2).trim());
    }
    return out;
  }

  /* One attempt. `signal(frame)` puts a frame on the relay control socket
     and returns false if it could not; `onReady(sock)` gets the open,
     authenticated DirectSocket; `onFail(reason)` is called at most once,
     and never after onReady. */
  class Link {
    constructor(opts) {
      this.id = opts.id || Math.random().toString(36).slice(2, 12);
      this.stun = Array.isArray(opts.stun) ? opts.stun : [];
      this.signal = opts.signal;
      this.onReady = opts.onReady || (() => {});
      this.onFail = opts.onFail || (() => {});
      this.RTC = opts.RTC || globalThis.RTCPeerConnection;
      // Called as this.setTimer(...), so the browser's own timers are wrapped:
      // window.setTimeout called on another object throws "Illegal
      // invocation", which left every negotiation stuck (claunch-k1z4z).
      this.setTimer = opts.setTimeout || ((fn, ms) => setTimeout(fn, ms));
      this.clearTimer = opts.clearTimeout || ((t) => clearTimeout(t));
      this.gatherCap = opts.gatherCapMs != null ? opts.gatherCapMs : GATHER_CAP_MS;
      this.readyTimeout = opts.readyTimeoutMs != null ? opts.readyTimeoutMs : READY_TIMEOUT_MS;
      this.pc = null;
      this.dc = null;
      this.sock = null;
      this.nonce = null;
      this.offered = null;     // candidate lines already inside the offer
      this.early = [];         // candidates found before the offer went out
      this.done = false;       // ready or failed: nothing more happens
      this.ready = false;
      this.timer = null;
      this.t0 = Date.now();
      this.phases = {};
    }

    _phase(name) { this.phases[name] = Date.now() - this.t0; }

    async start() {
      try {
        this.pc = new this.RTC({ iceServers: this.stun.map((u) => ({ urls: u })) });
        this.dc = this.pc.createDataChannel(LABEL, { ordered: true });
        this.sock = new DirectSocket(this.dc);
        this.dc.onopen = () => this._channelOpen();
        this.sock.onclose = () => this.fail("datachannel closed before ready");
        this.pc.onicecandidate = (ev) => this._candidate(ev.candidate);
        this.pc.onconnectionstatechange = () => {
          const state = this.pc && this.pc.connectionState;
          if (state !== "failed" && state !== "closed") return;
          // After ready the socket is app.js's: report it closed at once
          // rather than when the DataChannel's own close arrives, which a
          // browser may only notice after consent checks time out.
          if (this.ready) this.sock._closed(1006, "ice " + state);
          else this.fail("ice " + state);
        };
        this.timer = this.setTimer(() => this.fail("no channel in time"), this.readyTimeout);
        await this.pc.setLocalDescription(await this.pc.createOffer());
        await this._gathered();
        if (this.done) return;
        const sdp = this.pc.localDescription.sdp;
        this.offered = sdpCandidates(sdp);
        this._phase("offer");
        if (!this.signal({ type: "p2p_offer", id: this.id, sdp })) {
          this.fail("relay socket down");
          return;
        }
        const early = this.early;
        this.early = null;
        for (const c of early) this._candidate(c);
      } catch (err) {
        this.fail("offer failed: " + (err && err.message || err));
      }
    }

    /* Gathering complete or the cap, whichever comes first. */
    _gathered() {
      if (this.pc.iceGatheringState === "complete") return Promise.resolve();
      return new Promise((resolve) => {
        const t = this.setTimer(finish, this.gatherCap);
        const pc = this.pc;
        const prev = pc.onicegatheringstatechange;
        function finish() {
          pc.onicegatheringstatechange = prev;
          resolve();
        }
        pc.onicegatheringstatechange = () => {
          if (pc.iceGatheringState === "complete") { this.clearTimer(t); finish(); }
        };
      });
    }

    _candidate(cand) {
      if (this.done) return;
      if (this.early) { this.early.push(cand); return; }
      if (cand && cand.candidate) {
        if (this.offered && this.offered.has(cand.candidate.trim())) return;
        this.signal({ type: "p2p_ice", id: this.id, candidate: {
          candidate: cand.candidate, sdpMid: cand.sdpMid, sdpMLineIndex: cand.sdpMLineIndex } });
      } else if (cand === null || (cand && !cand.candidate)) {
        this.signal({ type: "p2p_ice", id: this.id, candidate: null });
      }
    }

    /* A p2p_answer / p2p_error from the relay socket. Returns true if it was
       this link's. */
    handle(msg) {
      if (!msg || msg.id !== this.id || this.done) return false;
      if (msg.type === "p2p_error") { this.fail("daemon: " + msg.error); return true; }
      if (msg.type !== "p2p_answer") return false;
      this.nonce = msg.nonce;
      this._phase("answer");
      this.pc.setRemoteDescription({ type: "answer", sdp: msg.sdp })
        .catch((err) => this.fail("answer refused: " + (err && err.message || err)));
      return true;
    }

    _channelOpen() {
      if (this.done) return;
      this._phase("open");
      if (!this.nonce) { this.fail("channel open before the answer"); return; }
      this.sock._open();
      this.sock.onmessage = (ev) => {
        let msg = null;
        try { msg = JSON.parse(ev.data); } catch { /* not the init */ }
        if (!msg || msg.type !== "init") return;
        this._phase("ready");
        this.done = true;
        this.ready = true;
        this.clearTimer(this.timer);
        this.sock.onmessage = null;
        this.sock.onclose = null;
        this.onReady(this.sock, this);
      };
      try {
        this.sock.send(this.nonce);
      } catch (err) {
        this.fail("nonce not sent");
      }
      this.nonce = null;
    }

    fail(reason) {
      if (this.done) return;
      this.done = true;
      // Nothing on the way may keep onFail from being called: without it
      // the page waits on this link for good and never tries again.
      try { this.clearTimer(this.timer); } catch { /* the timer is moot */ }
      try { this.close(); } catch { /* best effort */ }
      this.onFail(reason, this);
    }

    close() {
      try { this.signal({ type: "p2p_bye", id: this.id }); } catch { /* relay down */ }
      if (this.sock) { this.sock.onclose = null; this.sock.close(); }
      try { if (this.pc) this.pc.close(); } catch { /* already closed */ }
    }
  }

  globalThis.ClaunchP2P = { encode, Reassembler, DirectSocket, Link, FRAGMENT };
})();
