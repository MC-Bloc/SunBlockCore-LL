  const VC_KEYS = [
    'over_voltage_disconnect_voltage','charging_limit_voltage','over_voltage_reconnect_voltage',
    'equalize_charging_voltage','boost_charging_voltage','float_charging_voltage',
    'boost_reconnect_charging_voltage','low_voltage_reconnect_voltage','under_voltage_recover_voltage',
    'under_voltage_warning_voltage','low_voltage_disconnect_voltage','discharging_limit_voltage',
  ];

  const CHART_MAX = 300; // 5 minutes at 1s
  const CHART_H   = 160;

  // All plottable live fields — used to populate the "Add Chart" dropdown
  const CHART_VARS = {
    PVVoltage:          { label: 'PV Voltage',          unit: 'V',   color: '#60a5fa' },
    PVCurrent:          { label: 'PV Current',          unit: 'A',   color: '#a78bfa' },
    BattVoltage:        { label: 'Battery Voltage',     unit: 'V',   color: '#34d399' },
    BattTemperature:    { label: 'Battery Temperature', unit: '°C',  color: '#fb923c' },
    BattChargePower:    { label: 'Battery Charge Power',unit: 'W',   color: '#4ade80' },
    LoadPower:          { label: 'Load Power',          unit: 'W',   color: '#f87171' },
    BattOverallCurrent: { label: 'Battery Current',     unit: 'A',   color: '#c084fc' },
    CPUPowerDraw:       { label: 'CPU Power Draw',      unit: 'W',   color: '#94a3b8' },
  };

  const PLOTLY_LAYOUT = (yTitle, yRange) => ({
    height: CHART_H,
    margin: { t: 8, r: 16, b: 40, l: 52 },
    paper_bgcolor: 'transparent',
    plot_bgcolor:  'transparent',
    font:  { color: '#64748b', size: 11 },
    xaxis: {
      type: 'date',
      color: '#64748b',
      gridcolor: '#2a2d3a',
      tickcolor: '#2a2d3a',
      linecolor: '#2a2d3a',
      tickfont:  { size: 10 },
      showspikes: true, spikecolor: '#64748b', spikemode: 'across', spikesnap: 'cursor',
    },
    yaxis: {
      title: { text: yTitle, font: { size: 10 }, standoff: 4 },
      color: '#64748b',
      gridcolor: '#2a2d3a',
      tickcolor: '#2a2d3a',
      linecolor: '#2a2d3a',
      tickfont:  { size: 10 },
      ...(yRange ? { range: yRange } : {}),
    },
    hoverlabel: { bgcolor: '#1a1d27', bordercolor: '#2a2d3a', font: { color: '#e2e8f0', size: 11 } },
    hovermode: 'x unified',
    showlegend: false,
  });

  const PLOTLY_CONFIG = {
    displaylogo: false,
    responsive: true,
    modeBarButtonsToRemove: ['select2d', 'lasso2d', 'autoScale2d'],
    toImageButtonOptions: { format: 'png', scale: 2 },
  };

  document.addEventListener('alpine:init', () => {
    Alpine.data('sunblock', (initialAuth, initialSimMode, initialEnvDefaults, initialDataDir, initialAdminMode, initialVizFields) => ({

      // ── State ──
      tab:          'live',
      connected:    false,
      authed:       initialAuth,
      simMode:      initialSimMode,
      envDefaults:  initialEnvDefaults,
      dataDirectory: initialDataDir,
      dataDirDraft:  initialDataDir,
      adminMode:    initialAdminMode,
      live:         null,

      // Charts
      chartTimes: [], chartBatt: [], chartPV: [],
      liveHistory: {},           // rolling buffer for every CHART_VARS field
      chartsReady: false,
      extraCharts: [],           // user-added charts: [{ id, key, label, unit, color }]
      addChartKey: 'PVVoltage',  // currently selected variable in the dropdown
      CHART_VARS,                // expose to template for x-for

      // Parameters
      params: null, ctrlStatus: null,

      // Energy
      stats: null,

      // Settings
      settingsDraft: { read_interval: 1, data_man: true, sim_mode: false, token_expire_hours: 24 },
      settingsMsg: '', settingsMsgOk: true, settingsMsgDataDir: false,
      pwForm: { current: '', next: '', confirm: '' },
      pwMsg: '', pwMsgOk: true,

      // API tokens
      tokens: [],
      tokenForm: { name: '', expires_in_hours: 720 },
      tokenMsg: '', tokenMsgOk: true,
      newToken: null,  // { id, name, token } — shown once, right after creation

      // History
      history:    null,
      histFilter: { from: '', to: '', limit: 100, order: 'desc' },
      histLoading: false,

      // Login modal
      loginOpen:  false,
      loginUser:  '',
      loginPass:  '',
      loginError: '',
      loginStep:  'credentials',  // 'credentials' | '2fa'
      login2faCode: '',

      // Two-factor authentication (Settings tab)
      twofa: { enabled: false, backup_codes_remaining: 0 },
      twofaMsg: '', twofaMsgOk: true,
      twofaSetup: null,        // { secret, otpauth_uri } — during enrollment
      twofaConfirmCode: '',
      twofaBackupCodes: null,  // string[] — shown once, after confirm/regenerate
      twofaDisableForm: { password: '', code: '' },
      twofaRegenCode: '',

      // Visualize
      vizFields:   initialVizFields,
      vizSelected: Object.fromEntries(
        Object.keys(initialVizFields).map((k, i) =>
          [k, ['BattPercentage', 'PVPower', 'LoadPower'].includes(k)]
        )
      ),
      vizFilter: {
        from: '', to: '',
        sample: 5,
        smooth: 15, smoothEnabled: false,
        filterSpikes: true,
      },
      vizLoading: false,
      vizHasData: false,
      vizInfo:    null,
      vizError:   '',

      // Edit modal
      editOpen:  false,
      editForm:  { battery_capacity: null, temperature_compensation_coefficient: null, voltage_controls: {} },
      editError: '',
      vcKeys:    VC_KEYS,

      // ── Init ──
      init() {
        this.connectSocket();
        this.$watch('loginOpen', open => { if (open) this.$nextTick(() => this.$refs.loginUserInput?.focus()); });
        this.$nextTick(() => this.initCharts());

        // /admin entry-point: go straight to settings if already authed, else open login
        if (this.adminMode) {
          if (this.authed) this.switchTab('settings');
          else             this.loginOpen = true;
        }
      },

      // ── Connection ──
      connectSocket() {
        const socket = io();
        socket.on('connect',    () => this.connected = true);
        socket.on('disconnect', () => this.connected = false);
        socket.on('solar_data', d => this.onLiveData(d));
      },

      onLiveData(d) {
        this.live = d;
        const t = Math.floor(Date.now() / 1000);
        this.chartTimes.push(t);
        this.chartBatt.push(d.BattPercentage);
        this.chartPV.push(d.PVPower);
        // Keep a rolling buffer for every extra-chart-eligible field
        for (const key of Object.keys(CHART_VARS)) {
          if (!this.liveHistory[key]) this.liveHistory[key] = [];
          this.liveHistory[key].push(d[key] ?? null);
          if (this.liveHistory[key].length > CHART_MAX) this.liveHistory[key].shift();
        }
        if (this.chartTimes.length > CHART_MAX) {
          this.chartTimes.shift(); this.chartBatt.shift(); this.chartPV.shift();
        }
        this.updateCharts();
        this.updateExtraCharts(d);
      },

      // ── Charts ──
      initCharts() {
        if (typeof Plotly === 'undefined') return;

        const battTrace = {
          x: [], y: [],
          type: 'scatter', mode: 'lines',
          line: { color: '#10b981', width: 2 },
          fill: 'tozeroy', fillcolor: 'rgba(16,185,129,0.08)',
          hovertemplate: '%{y:.1f}%<extra></extra>',
        };
        const pvTrace = {
          x: [], y: [],
          type: 'scatter', mode: 'lines',
          line: { color: '#f59e0b', width: 2 },
          fill: 'tozeroy', fillcolor: 'rgba(245,158,11,0.08)',
          hovertemplate: '%{y:.1f} W<extra></extra>',
        };

        Plotly.newPlot('chart-batt', [battTrace], PLOTLY_LAYOUT('%', [0, 100]), PLOTLY_CONFIG);
        Plotly.newPlot('chart-pv',   [pvTrace],   PLOTLY_LAYOUT('W'),           PLOTLY_CONFIG);
        this.chartsReady = true;
      },

      updateCharts() {
        if (!this.chartsReady) return;
        const times = this.chartTimes.map(t => new Date(t * 1000));
        Plotly.extendTraces('chart-batt', { x: [[times.at(-1)]], y: [[this.chartBatt.at(-1)]] }, [0]);
        Plotly.extendTraces('chart-pv',   { x: [[times.at(-1)]], y: [[this.chartPV.at(-1)]]   }, [0]);
        // Trim to rolling window
        if (this.chartTimes.length > CHART_MAX) {
          const keep = { x: [times.slice(-CHART_MAX)], y: [this.chartBatt.slice(-CHART_MAX)] };
          Plotly.restyle('chart-batt', keep, [0]);
          Plotly.restyle('chart-pv',   { x: [times.slice(-CHART_MAX)], y: [this.chartPV.slice(-CHART_MAX)] }, [0]);
        }
      },

      // ── Extra charts ──
      addChart() {
        const cfg = CHART_VARS[this.addChartKey];
        if (!cfg) return;
        const id = 'chart-extra-' + Date.now();
        this.extraCharts.push({ id, key: this.addChartKey, ...cfg });

        // Backfill from existing rolling buffer then initialise Plotly
        const key = this.addChartKey;
        this.$nextTick(() => {
          const times  = this.chartTimes.map(t => new Date(t * 1000));
          const values = times.map((_, i) => {
            // chartBatt/chartPV are stored separately; others come from live history
            if (key === 'BattPercentage') return this.chartBatt[i];
            if (key === 'PVPower')        return this.chartPV[i];
            return this.liveHistory[key]?.[i] ?? null;
          });
          const trace = {
            x: times, y: values,
            type: 'scatter', mode: 'lines',
            line: { color: cfg.color, width: 2 },
            fill: 'tozeroy', fillcolor: cfg.color + '14',
            hovertemplate: '%{y:.2f} ' + cfg.unit + '<extra></extra>',
          };
          Plotly.newPlot(id, [trace], PLOTLY_LAYOUT(cfg.unit), PLOTLY_CONFIG);
        });
      },

      removeChart(id) {
        Plotly.purge(id);
        this.extraCharts = this.extraCharts.filter(c => c.id !== id);
      },

      updateExtraCharts(d) {
        if (!this.chartsReady || !this.extraCharts.length) return;
        const t = new Date();
        this.extraCharts.forEach(chart => {
          const val = d[chart.key];
          if (val == null) return;
          Plotly.extendTraces(chart.id, { x: [[t]], y: [[val]] }, [0]);
          // Trim to rolling window
          if (this.chartTimes.length >= CHART_MAX) {
            const times  = this.chartTimes.map(s => new Date(s * 1000));
            const values = this.liveHistory[chart.key]?.slice(-CHART_MAX) ?? [];
            Plotly.restyle(chart.id, { x: [times], y: [values] }, [0]);
          }
        });
      },

      // ── Auth ──
      battColor() {
        if (!this.live) return 'var(--muted)';
        const p = this.live.BattPercentage;
        return p < 20 ? 'var(--red)' : p < 40 ? 'var(--accent)' : 'var(--green)';
      },

      async doLogin() {
        this.loginError = '';
        if (!this.loginUser || !this.loginPass) { this.loginError = 'Enter username and password.'; return; }
        try {
          const res = await fetch('/api/login', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ username: this.loginUser, password: this.loginPass }),
          });
          const data = await res.json().catch(() => ({}));
          if (res.ok && data.requires_2fa) {
            // Password accepted — server holds a short-lived pending challenge
            // (sb_2fa_pending cookie); now prompt for the second factor.
            this.loginStep = '2fa';
            this.login2faCode = '';
            this.loginError = '';
          } else if (res.ok) {
            this.authed = true;
            this.closeLogin();
            // After login always land in settings
            this.switchTab('settings');
          } else {
            this.loginError = 'Invalid username or password.';
          }
        } catch { this.loginError = 'Connection error.'; }
      },

      async doVerify2FA() {
        this.loginError = '';
        const code = this.login2faCode.trim();
        if (!code) { this.loginError = 'Enter the 6-digit code from your authenticator app (or a backup code).'; return; }
        try {
          const res = await fetch('/api/login/verify-2fa', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ code }),
          });
          const data = await res.json().catch(() => ({}));
          if (res.ok) {
            this.authed = true;
            this.closeLogin();
            this.switchTab('settings');
          } else {
            this.loginError = data.detail || 'Invalid or expired code.';
          }
        } catch { this.loginError = 'Connection error.'; }
      },

      closeLogin() {
        this.loginOpen = false;
        this.loginUser = this.loginPass = this.login2faCode = '';
        this.loginError = '';
        this.loginStep = 'credentials';
      },

      async doLogout() {
        await fetch('/api/logout', { method: 'POST' });
        this.authed = false;
        this.tab = 'live';  // public users only see Live
      },

      // ── Profile ──
      async setProfile(profile) {
        const routes = { 'performance': '/api/performance-mode', 'balanced': '/api/balanced', 'power-saver': '/api/power-saver-mode' };
        const res = await fetch(routes[profile], { method: 'POST' });
        if (res.status === 401) { this.authed = false; this.tab = 'live'; }
      },

      // ── Tab switch ──
      switchTab(name) {
        // Guard: non-live tabs require auth
        if (name !== 'live' && !this.authed) return;
        this.tab = name;
        if (name === 'parameters' && !this.params)   this.loadParams();
        if (name === 'energy'     && !this.stats)    this.loadStats();
        if (name === 'settings')                     { this.loadSettings(); this.loadTokens(); this.loadTwoFAStatus(); }
        if (name === 'history'    && !this.history)  this.loadHistory(0);
      },

      // ── Visualize ──
      async plotViz() {
        const selectedFields = Object.keys(this.vizSelected).filter(k => this.vizSelected[k]);
        if (!selectedFields.length) { this.vizError = 'Select at least one variable.'; return; }
        this.vizError   = '';
        this.vizLoading = true;

        try {
          const p = new URLSearchParams({ fields: selectedFields.join(','), sample: this.vizFilter.sample });
          if (this.vizFilter.from) p.set('from', this.vizFilter.from);
          if (this.vizFilter.to)   p.set('to',   this.vizFilter.to);
          if (this.vizFilter.smoothEnabled && this.vizFilter.smooth > 1)
            p.set('smooth', this.vizFilter.smooth);
          p.set('filter_spikes', this.vizFilter.filterSpikes);

          const res  = await fetch('/api/data/visualize?' + p);
          const data = await res.json();

          if (res.status === 401) { this.authed = false; this.tab = 'live'; return; }
          if (!res.ok) { this.vizError = data.detail || 'Failed to load data.'; return; }
          if (!data.timestamps.length) { this.vizError = 'No data found for this range.'; return; }

          const palette = ['#10b981','#f59e0b','#60a5fa','#f87171','#a78bfa',
                           '#fb923c','#34d399','#c084fc','#94a3b8','#4ade80'];

          const traces = Object.entries(data.series).map(([key, series], i) => ({
            x: data.timestamps,
            y: series.values,
            name: `${series.label} (${series.unit})`,
            type: 'scatter',
            mode: 'lines',
            line: { color: palette[i % palette.length], width: 1.5 },
            hovertemplate: `%{y:.2f} ${series.unit}<extra>${series.label}</extra>`,
          }));

          const layout = {
            height: 420,
            margin: { t: 16, r: 32, b: 56, l: 60 },
            paper_bgcolor: 'transparent',
            plot_bgcolor:  'transparent',
            font:  { color: '#64748b', size: 11 },
            xaxis: {
              type: 'date',
              color: '#64748b', gridcolor: '#2a2d3a',
              tickcolor: '#2a2d3a', linecolor: '#2a2d3a',
              tickfont: { size: 10 },
            },
            yaxis: {
              color: '#64748b', gridcolor: '#2a2d3a',
              tickcolor: '#2a2d3a', linecolor: '#2a2d3a',
              tickfont: { size: 10 },
            },
            legend: {
              bgcolor: 'rgba(26,29,39,0.85)', bordercolor: '#2a2d3a', borderwidth: 1,
              font: { color: '#e2e8f0', size: 11 }, orientation: 'h',
              y: -0.18, x: 0,
            },
            hoverlabel: { bgcolor: '#1a1d27', bordercolor: '#2a2d3a', font: { color: '#e2e8f0', size: 11 } },
            hovermode: 'x unified',
            showlegend: true,
          };

          this.vizHasData = true;
          this.$nextTick(() => Plotly.react('chart-viz', traces, layout, PLOTLY_CONFIG));

          this.vizInfo = { total_rows: data.total_rows, sampled_rows: data.sampled_rows };
        } catch (e) {
          this.vizError = 'Connection error.';
        } finally {
          this.vizLoading = false;
        }
      },

      // ── Parameters ──
      async loadParams() {
        const [pr, sr] = await Promise.all([
          fetch('/api/controller/parameters'),
          fetch('/api/controller/status'),
        ]);
        if (pr.ok) this.params     = await pr.json();
        if (sr.ok) this.ctrlStatus = await sr.json();
      },

      openEdit() {
        if (!this.params) return;
        this.editError = '';
        this.editForm = {
          battery_capacity: this.params.battery_capacity,
          temperature_compensation_coefficient: this.params.temperature_compensation_coefficient,
          voltage_controls: { ...this.params.voltage_controls },
        };
        this.editOpen = true;
      },

      async saveParams() {
        this.editError = '';
        try {
          const res = await fetch('/api/controller/parameters', {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(this.editForm),
          });
          if (res.ok) {
            this.params = await res.json();
            this.editOpen = false;
          } else if (res.status === 401) {
            this.authed = false; this.editOpen = false; this.tab = 'live';
          } else {
            const err = await res.json().catch(() => ({}));
            this.editError = err.detail || 'Failed to save.';
          }
        } catch { this.editError = 'Connection error.'; }
      },

      async syncRtc() {
        const res = await fetch('/api/controller/rtc/sync', { method: 'POST' });
        if (res.status === 401) { this.authed = false; this.tab = 'live'; }
        else if (res.ok) this.loadParams();
      },

      // ── Energy ──
      async loadStats() {
        const res = await fetch('/api/controller/stats');
        if (res.ok) this.stats = await res.json();
      },

      // ── Settings ──
      async loadSettings() {
        if (!this.authed) return;
        const res = await fetch('/api/settings');
        if (res.ok) {
          const data = await res.json();
          this.settingsDraft  = data;
          this.simMode        = data.sim_mode;
          this.dataDirectory  = data.data_directory;
          this.dataDirDraft   = data.data_directory;
        }
      },

      async saveDataDir() {
        this.settingsMsg = '';
        this.settingsMsgDataDir = true;
        const d = this.dataDirDraft.trim();
        if (!d) { this.settingsMsgOk = false; this.settingsMsg = 'Path cannot be empty.'; return; }
        try {
          const res = await fetch('/api/settings', {
            method: 'PATCH',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ data_directory: d }),
          });
          const data = await res.json();
          if (res.ok) {
            this.dataDirectory  = data.data_directory;
            this.dataDirDraft   = data.data_directory;
            this.settingsDraft  = data;
            this.simMode        = data.sim_mode;
            this.settingsMsgOk  = true;
            this.settingsMsg    = 'Data directory saved.';
            setTimeout(() => { if (this.settingsMsg === 'Data directory saved.') this.settingsMsg = ''; }, 3000);
          } else {
            this.settingsMsgOk = false;
            this.settingsMsg   = data.detail || 'Failed to save.';
          }
        } catch { this.settingsMsgOk = false; this.settingsMsg = 'Connection error.'; }
      },

      async saveSetting(key, value) {
        this.settingsMsg = '';
        this.settingsMsgDataDir = false;
        try {
          const res = await fetch('/api/settings', {
            method: 'PATCH',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ [key]: value }),
          });
          const data = await res.json();
          if (res.ok) {
            this.settingsDraft  = data;
            this.simMode        = data.sim_mode;
            this.dataDirectory  = data.data_directory;
            this.dataDirDraft   = data.data_directory;
            this.settingsMsgOk  = true;
            this.settingsMsg    = 'Saved.';
            setTimeout(() => { if (this.settingsMsg === 'Saved.') this.settingsMsg = ''; }, 2500);
          } else {
            // revert draft to actual server state on failure
            await this.loadSettings();
            this.settingsMsgOk = false;
            this.settingsMsg = data.detail || 'Failed to save.';
          }
        } catch {
          this.settingsMsgOk = false;
          this.settingsMsg = 'Connection error.';
        }
      },

      async resetSetting(key) {
        this.settingsMsg = '';
        this.settingsMsgDataDir = false;
        try {
          const res = await fetch(`/api/settings/${key}`, { method: 'DELETE' });
          const data = await res.json();
          if (res.ok) {
            this.settingsDraft  = data;
            this.simMode        = data.sim_mode;
            this.dataDirectory  = data.data_directory;
            this.dataDirDraft   = data.data_directory;
            this.settingsMsgOk  = true;
            this.settingsMsg    = 'Reset to .env default.';
            setTimeout(() => { if (this.settingsMsg === 'Reset to .env default.') this.settingsMsg = ''; }, 2500);
          } else {
            await this.loadSettings();
            this.settingsMsgOk = false;
            this.settingsMsg = data.detail || 'Reset failed.';
          }
        } catch { this.settingsMsgOk = false; this.settingsMsg = 'Connection error.'; }
      },

      async changePassword() {
        this.pwMsg = '';
        if (!this.pwForm.current) { this.pwMsgOk = false; this.pwMsg = 'Enter your current password.'; return; }
        if (this.pwForm.next !== this.pwForm.confirm) { this.pwMsgOk = false; this.pwMsg = 'New passwords do not match.'; return; }
        if (this.pwForm.next.length < 8) { this.pwMsgOk = false; this.pwMsg = 'Password must be at least 8 characters.'; return; }
        try {
          const res = await fetch('/api/settings/password', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ current_password: this.pwForm.current, new_password: this.pwForm.next }),
          });
          const data = await res.json();
          if (res.ok) {
            this.pwMsgOk = true;
            this.pwMsg = 'Password updated.';
            this.pwForm = { current: '', next: '', confirm: '' };
          } else {
            this.pwMsgOk = false;
            this.pwMsg = data.detail || 'Failed.';
          }
        } catch { this.pwMsgOk = false; this.pwMsg = 'Connection error.'; }
      },

      async loadTokens() {
        try {
          const res = await fetch('/api/tokens');
          if (res.ok) this.tokens = (await res.json()).tokens;
        } catch {}
      },
      async createToken() {
        this.tokenMsg = '';
        this.newToken = null;
        const name = this.tokenForm.name.trim();
        if (!name) { this.tokenMsgOk = false; this.tokenMsg = 'Enter a name for the token.'; return; }
        try {
          const res = await fetch('/api/tokens', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
              name,
              expires_in_hours: this.tokenForm.expires_in_hours || null,
            }),
          });
          const data = await res.json();
          if (res.ok) {
            this.tokenMsgOk = true;
            this.tokenMsg = '';
            this.newToken = { id: data.id, name: data.name, token: data.token };
            this.tokenForm.name = '';
            await this.loadTokens();
          } else {
            this.tokenMsgOk = false;
            this.tokenMsg = data.detail || 'Failed.';
          }
        } catch { this.tokenMsgOk = false; this.tokenMsg = 'Connection error.'; }
      },
      async revokeToken(id) {
        if (!confirm('Revoke this token? Anything using it will immediately lose API access.')) return;
        try {
          const res = await fetch(`/api/tokens/${id}`, { method: 'DELETE' });
          if (res.ok) {
            if (this.newToken && this.newToken.id === id) this.newToken = null;
            await this.loadTokens();
          } else {
            const data = await res.json();
            this.tokenMsgOk = false;
            this.tokenMsg = data.detail || 'Failed to revoke token.';
          }
        } catch { this.tokenMsgOk = false; this.tokenMsg = 'Connection error.'; }
      },
      async copyToken(value) {
        try { await navigator.clipboard.writeText(value); } catch {}
      },

      // Two-factor authentication
      async loadTwoFAStatus() {
        try {
          const res = await fetch('/api/2fa/status');
          if (res.ok) this.twofa = await res.json();
        } catch {}
      },
      async startTwoFASetup() {
        this.twofaMsg = '';
        this.twofaConfirmCode = '';
        try {
          const res = await fetch('/api/2fa/setup', { method: 'POST' });
          const data = await res.json();
          if (res.ok) {
            this.twofaSetup = { secret: data.secret, otpauth_uri: data.otpauth_uri };
            this.$nextTick(() => this.renderTwoFAQr());
          } else {
            this.twofaMsgOk = false;
            this.twofaMsg = data.detail || 'Failed to start 2FA setup.';
          }
        } catch { this.twofaMsgOk = false; this.twofaMsg = 'Connection error.'; }
      },
      renderTwoFAQr() {
        const el = this.$refs.twofaQr;
        if (!el || !window.QRCode || !this.twofaSetup) return;
        el.innerHTML = '';
        new QRCode(el, { text: this.twofaSetup.otpauth_uri, width: 176, height: 176 });
      },
      cancelTwoFASetup() {
        this.twofaSetup = null;
        this.twofaConfirmCode = '';
        this.twofaMsg = '';
      },
      async confirmTwoFA() {
        this.twofaMsg = '';
        const code = this.twofaConfirmCode.trim();
        if (!code) { this.twofaMsgOk = false; this.twofaMsg = 'Enter the 6-digit code from your authenticator app.'; return; }
        try {
          const res = await fetch('/api/2fa/confirm', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ code }),
          });
          const data = await res.json();
          if (res.ok) {
            this.twofaSetup = null;
            this.twofaConfirmCode = '';
            this.twofaMsgOk = true;
            this.twofaMsg = '';
            this.twofaBackupCodes = data.backup_codes;
          } else {
            this.twofaMsgOk = false;
            this.twofaMsg = data.detail || 'Incorrect code.';
          }
        } catch { this.twofaMsgOk = false; this.twofaMsg = 'Connection error.'; }
      },
      async disableTwoFA() {
        this.twofaMsg = '';
        if (!this.twofaDisableForm.password || !this.twofaDisableForm.code) {
          this.twofaMsgOk = false;
          this.twofaMsg = 'Enter your password and a current 2FA code.';
          return;
        }
        if (!confirm('Disable two-factor authentication? This removes the second layer of protection on the admin account.')) return;
        try {
          const res = await fetch('/api/2fa/disable', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ password: this.twofaDisableForm.password, code: this.twofaDisableForm.code }),
          });
          const data = await res.json();
          if (res.ok) {
            this.twofaDisableForm = { password: '', code: '' };
            this.twofaMsgOk = true;
            this.twofaMsg = 'Two-factor authentication disabled.';
            await this.loadTwoFAStatus();
          } else {
            this.twofaMsgOk = false;
            this.twofaMsg = data.detail || 'Failed to disable 2FA.';
          }
        } catch { this.twofaMsgOk = false; this.twofaMsg = 'Connection error.'; }
      },
      async regenerateBackupCodes() {
        this.twofaMsg = '';
        const code = this.twofaRegenCode.trim();
        if (!code) { this.twofaMsgOk = false; this.twofaMsg = 'Enter a current 2FA code to regenerate backup codes.'; return; }
        try {
          const res = await fetch('/api/2fa/backup-codes/regenerate', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ code }),
          });
          const data = await res.json();
          if (res.ok) {
            this.twofaRegenCode = '';
            this.twofaMsg = '';
            this.twofaBackupCodes = data.backup_codes;
          } else {
            this.twofaMsgOk = false;
            this.twofaMsg = data.detail || 'Failed to regenerate backup codes.';
          }
        } catch { this.twofaMsgOk = false; this.twofaMsg = 'Connection error.'; }
      },

      fmt(v) { return v != null ? Number(v).toFixed(2) : '–'; },

      // ── History ──
      async loadHistory(offset) {
        if (offset === undefined) offset = 0;
        this.histLoading = true;
        try {
          const p = new URLSearchParams({
            limit:  this.histFilter.limit,
            offset: offset,
            order:  this.histFilter.order,
          });
          if (this.histFilter.from) p.set('from', this.histFilter.from);
          if (this.histFilter.to)   p.set('to',   this.histFilter.to);
          const res = await fetch('/api/data/history?' + p);
          if (res.status === 401) { this.authed = false; this.tab = 'live'; return; }
          if (res.ok) this.history = await res.json();
        } catch { /* swallow — server down */ }
        finally  { this.histLoading = false; }
      },

      histPage(dir) {
        if (!this.history) return;
        const next = this.history.offset + dir * this.history.limit;
        if (next < 0 || next >= this.history.total) return;
        this.loadHistory(next);
      },

    })); // Alpine.data
  }); // alpine:init
