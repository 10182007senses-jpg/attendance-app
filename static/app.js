
      function isLoggedIn() {
        return !!sessionStorage.getItem("session");
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
            body: JSON.stringify({ user: name, pin: pin })
          });
          const data = await res.json();

          if (data.ok) {
            sessionStorage.setItem("session", data.session);
            sessionStorage.setItem("user", data.user);
            setButtonsDisabled(false);
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
        const session = sessionStorage.getItem("session");
        try {
          if (session) {
            await fetch("/logout", {
              method: "POST",
              headers: { "Authorization": `Bearer ${session}` }
            });
          }
        } catch (e) {
        } finally {
          sessionStorage.removeItem("session");
          sessionStorage.removeItem("user");
          location.reload();
        }
      }

      function authHeaders() {
        const session = sessionStorage.getItem("session");
        return session ? {"Authorization": `Bearer ${session}`} : {};
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

      function getLocation() {
        return new Promise((resolve, reject) => {
          if (!navigator.geolocation) {
            reject("このブラウザは位置情報に対応していません");
          }

          navigator.geolocation.getCurrentPosition(
            pos => {
              resolve({
                lat: pos.coords.latitude,
                lon: pos.coords.longitude
              });
            },
            err => reject("位置情報を取得できません")
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
          const loc = await getLocation();
          const url = buildUrl(path, { lat: loc.lat, lon: loc.lon });

          const res = await fetchWithAuth(url);
          if (!res) return;
          const data = await res.json();
          if (data.status === "ok") {
            setStatus("記録しました。", "ok");
            await loadLogs();
            await loadCurrentState();
          } else {
            setStatus(data.error || "記録できませんでした。", "error");
          }
        } catch (err) {
          setStatus(err || "通信エラーが発生しました。", "error");
        } finally {
          setButtonsDisabled(false);
          await loadCurrentState();
        }
      }


      function updateButtons(state) {
        const btnIn = document.querySelector("button[onclick*='clock-in']");
        const btnOut = document.querySelector("button[onclick*='clock-out']");
        const btnBreakStart = document.querySelector("button[onclick*='break-start']");
        const btnBreakEnd = document.querySelector("button[onclick*='break-end']");

         // 全部一旦有効
        [btnIn, btnOut, btnBreakStart, btnBreakEnd].forEach(b => b.disabled = false);

        if (state === "未出勤") {
          btnOut.disabled = true;
          btnBreakStart.disabled = true;
          btnBreakEnd.disabled = true;
        }

        if (state === "出勤中") {
          btnIn.disabled = true;
          btnBreakEnd.disabled = true;
        }

        if (state === "休憩中") {
          btnIn.disabled = true;
          btnBreakStart.disabled = true;
          btnOut.disabled = true;
        }
      }

      async function loadCurrentState() {
        setStatus("状態を取得中...");
        try {
          const res = await fetchWithAuth(buildUrl("/current-state"));
          if (!res) return;

          const data = await res.json();

          const stateEl = document.getElementById("current-state");

          if (data.error) {
            stateEl.innerText = "?? 状態を取得できません";
            setStatus(data.error, "error");
            return;
          }

          if (data.state === "出勤中") {
            stateEl.innerText = "🟢 出勤中";
          } else if (data.state === "休憩中") {
            stateEl.innerText = "🟡 休憩中";
          } else {
            stateEl.innerText = "🔴 未出勤 / 退勤済み";
          }

          if (data.lat && data.lon) {
            document.getElementById("location-info").innerText = `最終打刻位置： (${data.lat.toFixed(5)}, ${data.lon.toFixed(5)})`;
          } else {
            document.getElementById("location-info").innerText = "";
          }

          updateButtons(data.state);
        } catch {
          document.getElementById("current-state").innerText = "状態を取得できません"
          setStatus("状態の取得に失敗しました。", "error");
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
            result.innerText = `実働 ${data.net_work_time}（休憩 ${data.break_time}）`;
            setStatus("勤務時間を更新しました。", "ok");
          } else {
            result.innerText = data.error || "データが足りません。";
            setStatus("勤務時間を計算できませんでした。", "error");
          }
        } catch (err) {
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

      applyLoginState();
      updateTodayLabel();
      loadLogs();
      loadCurrentState();
    
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
  const mergedHeaders = {
    ...normalizeHeaders(options.headers),
    ...authHeaders(),
  };

  let res;
  try {
    res = await fetch(url, { ...options, headers: mergedHeaders });
  } catch (e) {
    setStatus("通信エラーが発生しました。", "error");
    return null;
  }

  if (res.status === 401 || res.status === 403) {
    forceLogout("ログインが無効になりました。");
    return null;
  }

  const ct = res.headers.get("content-type") || "";
  if (ct.includes("application/json")) {
    const clone = res.clone();
    try {
      const body = await clone.json();
      if (body && (body.detail === "未ログイン" || body.error === "未ログイン" || body.errorA)) {
        forceLogout("ログインが無効になりました。");
        return null;
      }
    } catch (_) {}
  }

  return res;
}
