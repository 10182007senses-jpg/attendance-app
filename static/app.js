       function isLoggedIn() {
        return !!sessionStorage.getItem("user");
      }

      async function bootstrapAuth() {
        try{
          const res = await fetch("/me", {credentials: "include"})
          if (!res.ok) throw new Error;
          const data = await res.json()
          sessionStorage.setItem("user", data.user);
          sessionStorage.setItem("role", data.role || "");
        } catch {
          sessionStorage.removeItem("user");
          sessionStorage.removeItem("role");
        }
      }

      async function login() {
        const name = document.getElementById("login-name").value.trim();
        const pin = document.getElementById("login-pin").value.trim();

        if (!name || !pin) {
          setStatus("ユーザー名とPINを入力してください", "error");
          return ;
        }

        setStatus("ログイン中...", "info");

        try {
          const res = await fetch("/login", {
            method: "POST",
            headers: {"Content-Type": "application/json"},
            credentials: "include",
            body: JSON.stringify({ user: name, pin: pin })
          });
          const data = await res.json();

          if (data.ok) {
            sessionStorage.setItem("role", data.role || "");
            sessionStorage.setItem("user", data.user);
            setButtonsDisabled(true);
            setStatus(`${data.user}でログインしました`, "ok");
            applyLoginState();
            await loadLogs();
            await loadCurrentState();
          } else {
            setStatus(data.error || "ログインできませんでした", "error");
          }
        } catch (e) {
          setStatus("通信エラーが発生しました", "error");
        }
      }

      async function logout() {
        try {
            await fetch("/logout", {
              method: "POST",
              credentials: "include",
            });
        } catch (_) {
        } finally {
          sessionStorage.removeItem("role");
          sessionStorage.removeItem("user");
          location.reload();
        }
      }
      function authHeaders() {
        return {};
      }

      function applyLoginState() {
        const user = sessionStorage.getItem("user");

        const loginPanel = document.getElementById("login-panel");
        const userInput = document.querySelector(".user-input");
        const userNameInput = document.getElementById("user-name");
        const loginState = document.getElementById("login-state");
        const loginNameInput = document.getElementById("login-name");
        const loginPinInput = document.getElementById("login-pin");

        if (isLoggedIn()) {
          loginPanel.style.display = "none";
          if (userInput) userInput.style.display = "flex";
          if (userNameInput) userNameInput.value = user;
          if (userNameInput) userNameInput.disabled = true;
          if (loginState) loginState.innerText = `${user}でログイン中`;
          if (loginNameInput) loginNameInput.disabled = true;
          updateTodayLabel();
          loadLogs();
          loadCurrentState();
          loadWorkTime();
          loadMonthSummary();
        } else {
          loginPanel.style.display = "block";
          if (userInput) userInput.style.display = "none";
          if (userNameInput) userNameInput.disabled = false;
          if (loginState) loginState.innerText = "";
          if (loginNameInput) loginNameInput.value = "";
          if (loginNameInput) loginNameInput.disabled = false;
          if (loginPinInput) loginPinInput.value = "";
          document.getElementById("current-state").innerText = "未ログイン";
          document.getElementById("log-table").innerHTML = `<tr><td colspan="3">ログインしてください。</td></tr>`;

          setButtonsDisabled(true)
        }
      }

      const DEFAULT_USER = "瀬良 仁";
      const STATE_NOT_IN = "未入室";
      const STATE_IN_ROOM = "在室中";
      const STATE_UNKNOWN = "不明";


      function getUser() {
        const u = sessionStorage.getItem("user");
      }

      function buildUrl(path, params) {
        if (!params) {
          return path;
        }
        const url = new URL(path, window.location.origin);
        Object.entries(params).forEach(([key, value]) => {
          if (value !== undefined && value !== null) {
            url.searchParams.set(key, value);
          }
        });
        return url.pathname + url.search;
      }

      function setButtonsDisabled(disabled) {
        document.querySelectorAll(".actions button").forEach(btn => {
          btn.disabled = disabled;
        })
      }

      function getLocationOptional() {
        return new Promise(resolve => {
          if (!navigator.geolocation) {
            resolve({
              lat: null,
              lon: null,
              locationError: "このブラウザは位置情報に対応していません"
            });
            return;
          }

          navigator.geolocation.getCurrentPosition(
            pos => {
              resolve({
                lat: pos.coords.latitude,
                lon: pos.coords.longitude,
                locationError: null
              });
            },
            () => resolve({
              lat: null,
              lon: null,
              locationError: "位置情報を取得できませんでした"
            })
          );
        });
      }

      async function callApi(path) {
        if (!isLoggedIn()) {
          setStatus("ログインしてください", "error");
          return ;
        }
        setButtonsDisabled(true);
        setStatus("記録中...");
        try {
          const loc = await getLocationOptional();
          const url = buildUrl(path, { lat: loc.lat, lon: loc.lon });

          const res = await fetchWithAuth(url);
          if (!res) return;
          const data = await res.json();
          if (data.status === "ok") {
            if (loc.lat != null && loc.lon != null) {
              setStatus("位置情報付きで記録しました。", "ok");
            } else {
              setStatus("位置情報なしで記録しました。ブラウザで位置情報を許可すると位置付きで保存できます。", "ok");
            }
            await loadLogs();
            await loadCurrentState({ silent: true });
          } else {
            setStatus(data.error || "記録できませんでした。", "error");
          }
        } catch (err) {
          setStatus(err || "通信エラーが発生しました。", "error");
        } finally {
          setButtonsDisabled(true);
          await loadCurrentState({ silent: true });
        }
      }


      function isKnownState(state) {
        return state === STATE_NOT_IN || state === STATE_IN_ROOM || state === STATE_UNKNOWN;
      }

      function updateButtons(state) {
        const btnIn = document.querySelector("button[onclick*='clock-in']");
        const btnOut = document.querySelector("button[onclick*='clock-out']");

        [btnIn, btnOut].filter(Boolean).forEach(b => b.disabled = false);

        if (!isKnownState(state) || state === STATE_UNKNOWN) {
          [btnIn, btnOut].filter(Boolean).forEach(b => b.disabled = true);
          return;
        }

        if (state === STATE_NOT_IN) {
          if (btnOut) btnOut.disabled = true;
        }

        if (state === STATE_IN_ROOM) {
          if (btnIn) btnIn.disabled = true;
        }
      }

      async function loadCurrentState({ silent = false } = {}) {
        if (!silent) {
          setStatus("状態を取得中...");
        }
        updateButtons(null);
        try {
          const res = await fetchWithAuth(buildUrl("/current-state"));
          if (!res) {
            updateButtons(null);
            return;
          }

          const data = await res.json();

          const stateEl = document.getElementById("current-state");

          if (data.error) {
            stateEl.innerText = "?? 状態を取得できません";
            setStatus(data.error, "error");
            updateButtons(null);
            return;
          }

          if (data.state === STATE_IN_ROOM) {
            stateEl.innerText = "在室中";
          } else if (data.state === STATE_NOT_IN) {
            stateEl.innerText = "未入室 / 退室済み";
          } else {
            stateEl.innerText = "状態不明";
          }

          if (data.lat && data.lon) {
            document.getElementById("location-info").innerText = `最終打刻位置： (${data.lat.toFixed(5)}, ${data.lon.toFixed(5)})`;
          } else {
            document.getElementById("location-info").innerText = "";
          }

          updateButtons(data.state);
          if (!silent) {
            setStatus("状態を更新しました。", "ok");
          }
        } catch {
          document.getElementById("current-state").innerText = "状態を取得できません"
          setStatus("状態の取得に失敗しました。", "error");
          updateButtons(null);
        }
      }

      async function loadWorkTime() {
        setStatus("勤務時間を取得中...");
        try {
          const res = await fetchWithAuth(buildUrl("/work-time"));
          if (!res) return;
          const data = await res.json();
          const result = document.getElementById("result");
            if (data.net_work_time) {
              result.innerText = `実働 ${data.net_work_time}`;
            setStatus("勤務時間を更新しました。", "ok");
          } else {
            result.innerText = data.error || "データが足りません。";
            setStatus("勤務時間を計算できませんでした。", "error");
          }
        } catch (err) {
          setStatus("通信エラーが発生しました。", "error");
        }
      }

      function getCurrentMonthStr() {
        const now = new Date();
        const y = now.getFullYear();
        const m = String(now.getMonth() + 1).padStart(2, "0")
        return `${y}-${m}`;
      }

      async function loadMonthSummary() {
        setStatus("今月のサマリーを取得中...");

        try {
          const month = getCurrentMonthStr();
          const res = await fetchWithAuth(buildUrl("/me/month-summary", {month}));
          if (!res) return;

          const data = await res.json();

          const resultEl = document.getElementById("month-summary-result");
          const detailEl = document.getElementById("month-summary-detail");

          if (data.error) {
            resultEl.innerText = data.error;
            if (detailEl) detailEl.innerText = "";
            setStatus("サマリーを取得できませんでした", "error");
            return;
          }

          resultEl.innerText =
          `実働：${data.worked_time} / 所定：${data.required_hours}時間`;

          if (detailEl) {
            detailEl.innerText =
            `出勤日数：${data.worked_days}日（上限 ${data.max_work_days}日） / 残り：${data.remaining_time} / 残り出勤可能日：${data.remaining_days}日`;
          }

          setStatus("今月のサマリーを更新しました", "ok");
        } catch (e) { 
          setStatus("通信エラーが発生しました。", "error");
        }
      }

      async function loadLogs() {
        setStatus("ログを取得中...");
        try {
          const res = await fetchWithAuth(buildUrl("/today-logs"));
          if (!res) return;

          const data = await res.json();
          const table = document.getElementById("log-table");
          table.innerHTML = "";

          if (data.error) {
            const row = document.createElement("tr");
            row.innerHTML = `<td colspan="3">ログを取得できません。</td>`;
            table.appendChild(row);
            setStatus(data.error, "error");
            return;
          }

          if (!data.logs || data.logs.length === 0) {
            const row = document.createElement("tr");
            row.innerHTML = `<td colspan="3">今日のログはまだありません。</td>`;
            table.appendChild(row);
            setStatus("ログがありません。", "ok");
            return;
          }

          data.logs.forEach(log => {
            const row = document.createElement("tr");
            row.innerHTML = `
              <td>${log.ユーザー}</td>
              <td>${log.アクション}</td>
              <td>${log.時刻}</td>
            `;
            table.appendChild(row);
          });
          setStatus("ログを更新しました。", "ok");
        } catch (err) {
          setStatus("通信エラーが発生しました。", "error");
        }
      }

      
      function setStatus(text, type = "info") {
        const status = document.getElementById("status");
        status.className = "";
        if (type !== "info") {
          status.classList.add(type);
        }
        status.innerText = text;
      }

      function updateTodayLabel() {
        const today = new Date();
        const label = `${today.getFullYear()}年${today.getMonth() + 1}月${today.getDate()}日`;
        document.getElementById("today-label").innerText = label;
      }

      (async () => {
        await bootstrapAuth();
        applyLoginState();
        updateTodayLabel();
        loadLogs();
        loadCurrentState();        
      })();

    
async function forceLogout(msg="セッションが切れました。再ログインしてください。") {
  const session = sessionStorage.getItem("session")

  try {
    if (session) {
      await fetch("/logout", {
        method: "POST",
        headers: {"Authorization": `Bearer${session}`}
      });
    }
  } catch (_){

  } finally {
      sessionStorage.removeItem("session");
      sessionStorage.removeItem("user");
      setStatus(msg, "error");
      applyLoginState();
  }

}

function normalizeHeaders(h) {
  if (!h) return {};
  if (h instanceof Headers) return Object.fromEntries(h.entries());
  return h;
}

async function fetchWithAuth(url, options={}) {
  let res;
  try {
    res = await fetch(url, {
      ...options,
      credentials: "include",
      headers: {...(options.headers || {})},
    });
  } catch (e){
    setStatus("通信エラーが発生しました。", "error");
    return null;
  }

  if (res.status === 401 || res.status === 403) {
    await forceLogout("ログインが無効になりました。");
    return null;
  }
  return res
};
