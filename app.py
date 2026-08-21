import http.server
import socketserver
import json
import subprocess
import os
import re
import logging
import urllib.request

PORT = 5000
CONFIG_FILE = "config.json"
LOG_FILE = "UtilityHub.log"

# Set up logger to write to clearance.log
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.FileHandler(LOG_FILE, mode='a', encoding='utf-8'),
        logging.StreamHandler()  # Prints log messages directly to terminal
    ],
    force=True  # Overrides default logging setup
)

def get_config():
    if not os.path.exists(CONFIG_FILE):
        return {}
    with open(CONFIG_FILE, "r") as f:
        return json.load(f)

def save_config(data):
    with open(CONFIG_FILE, "w") as f:
        json.dump(data, f, indent=2)

def safe_replace(query_str, replacements):
    """Safely replace {key} placeholders without triggering KeyError on curly braces."""
    for key, value in replacements.items():
        query_str = query_str.replace(f"{{{key}}}", str(value))
    return query_str

def run_mysql_query(mod_name, cfg, sql_statements):
    if not cfg or "host" not in cfg:
        err = "Database configuration missing"
        logging.error(f"[{mod_name}] {err}")
        return {"status": "FAILED", "error": err}

    if not sql_statements:
        err = "No SQL queries defined in configuration"
        logging.error(f"[{mod_name}] {err}")
        return {"status": "FAILED", "error": err}

    # Log DB Configuration
    safe_cfg = {k: (v if k != "password" else "****") for k, v in cfg.items() if k != "queries" and not k.startswith("queries_")}
    logging.info(f"[{mod_name}] DB Config: {json.dumps(safe_cfg)}")

    failed_queries = []

    # Execute ALL queries sequentially
    for sql in sql_statements:
        sql = sql.strip()
        if not sql:
            continue

        logging.info(f"[{mod_name}] Executing Query: {sql}")

        cmd = [
            "mysql",
            "-vvv",  # Verbose level 3 forces full row output
            f"-h{cfg['host']}",
            f"-P{cfg['port']}",
            f"-u{cfg['user']}",
            f"-p{cfg['password']}",
            cfg['database'],
            "-e", f'"{sql}"'
        ]

        try:
            cmd_str = " ".join(cmd)
            res = subprocess.run(cmd_str, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=15)

            raw_stdout = res.stdout.strip()
            raw_stderr = res.stderr.strip()

            if raw_stdout:
                logging.info(f"[{mod_name}] MySQL Output:\n{raw_stdout}")

            # 1. Check for MySQL command execution errors
            if res.returncode != 0:
                err = raw_stderr or raw_stdout or "Execution error"
                logging.error(f"[{mod_name}] Query Failed: {err}")
                failed_queries.append(f"SQL Error: {err}")
                continue  # Continue to next query in module!

            # 2. Check if 0 rows were matched/updated
            if re.search(r"(Rows matched:\s*0|0 rows affected|Changed:\s*0)", raw_stdout, re.IGNORECASE) or "Query OK" not in raw_stdout:
                logging.warning(f"[{mod_name}] No matching rows found for query: {sql}")
                failed_queries.append(f"No rows found/updated for: {sql}")

        except Exception as e:
            logging.error(f"[{mod_name}] Exception on query '{sql}': {str(e)}")
            failed_queries.append(str(e))

    # Evaluate final module status after ALL queries run
    if failed_queries:
        summary_err = "; ".join(failed_queries)
        logging.error(f"[{mod_name}] Module overall status: FAILED ({summary_err})")
        return {"status": "FAILED", "error": summary_err}

    logging.info(f"[{mod_name}] Execution Successful - All queries updated rows")
    return {"status": "SUCCESS", "message": "All queries executed successfully"}

def run_esb_curl(curl_template, msisdn):
    if not curl_template:
        err = "ESB cURL configuration missing"
        logging.error(f"[ESB] {err}")
        return {"status": "FAILED", "error": err}

    primary_identity = msisdn[-9:] if len(msisdn) >= 9 else msisdn
    
    formatted_curl = safe_replace(curl_template, {
        "msisdn": msisdn,
        "primary_identity": primary_identity
    })

    # Unescape escaped quotes so the shell gets clean quotes
    formatted_curl = formatted_curl.replace('\\"', '"')

    # Add connection timeout flags (--connect-timeout 5 -m 10) directly to curl
    if "curl " in formatted_curl and "--connect-timeout" not in formatted_curl:
        formatted_curl = formatted_curl.replace("curl ", "curl --connect-timeout 5 -m 10 ", 1)

    logging.info(f"[ESB] Executing cURL Command: {formatted_curl}")

    try:
        res = subprocess.run(
            formatted_curl, 
            shell=True, 
            stdout=subprocess.PIPE, 
            stderr=subprocess.PIPE, 
            text=True, 
            timeout=12  # Slightly higher than curl's internal -m 10 limit
        )
        raw_stdout = res.stdout.strip()
        raw_stderr = res.stderr.strip()

        if raw_stdout:
            logging.info(f"[ESB] Output: {raw_stdout}")
        if raw_stderr:
            logging.info(f"[ESB] Stderr/Trace: {raw_stderr}")

        if res.returncode == 0:
            if "<faultcode>" in raw_stdout or ("ResultCode>0" not in raw_stdout and "ResultCode" in raw_stdout):
                err = "ESB returned SOAP Fault or Non-Zero ResultCode"
                logging.error(f"[ESB] {err}")
                return {"status": "FAILED", "error": err}

            logging.info("[ESB] Executed successfully")
            return {"status": "SUCCESS", "message": "ESB cURL executed successfully"}
        else:
            err = raw_stderr or raw_stdout or "cURL execution failed"
            logging.error(f"[ESB] Failed: {err}")
            return {"status": "FAILED", "error": err}

    except subprocess.TimeoutExpired:
        err = "ESB Endpoint unreachable / Connection timed out (10s)"
        logging.error(f"[ESB] {err}")
        return {"status": "FAILED", "error": err}
    except Exception as e:
        logging.error(f"[ESB] Exception occurred: {str(e)}")
        return {"status": "FAILED", "error": str(e)}

def query_ollama_qwen(user_prompt):
    ollama_url = "http://localhost:11434/api/generate"
    
    # System prompt enforcing strict Unix command generation
    # system_instruction = (
    #     "You are an expert Unix/Linux systems administrator. "
    #     "Provide direct, safe, production-grade Unix shell commands based on user requests. "
    #     "Include a brief 1-line explanation of flags used."
    # )

    payload = {
        "model": "qwen2.5-coder:7b",
        # "prompt": f"{system_instruction}\n\nUser Request: {user_prompt}",
        "prompt": f"{user_prompt}",
        "stream": False
    }
    logging.info(f"[AI Prompt] {json.dumps(payload)}")
    for h in logging.getLogger().handlers:
        h.flush()
    
    try:
        req = urllib.request.Request(
            ollama_url,
            data=json.dumps(payload).encode('utf-8'),
            headers={'Content-Type': 'application/json'}
        )
        with urllib.request.urlopen(req, timeout=120) as response:
            res_data = json.loads(response.read().decode('utf-8'))
            return {"status": "SUCCESS", "result": res_data.get("response", "")}
    except Exception as e:
        return {"status": "FAILED", "error": f"Ollama Connection Failed: {str(e)}"}

class UtilityHandler(http.server.SimpleHTTPRequestHandler):
    def do_GET(self):
        # 1. Main Landing Portal
        if self.path == "/" or self.path == "/index.html":
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            if os.path.exists("templates/portal.html"):
                with open("templates/portal.html", "rb") as f:
                    self.wfile.write(f.read())

        # 2. Asset Clearance App Route
        elif self.path == "/apps/clearance":
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            if os.path.exists("templates/clearance.html"):
                with open("templates/clearance.html", "rb") as f:
                    self.wfile.write(f.read())

        elif self.path == "/apps/game":
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            if os.path.exists("templates/game.html"):
                with open("templates/game.html", "rb") as f:
                    self.wfile.write(f.read())

        # 3. API Config Endpoint
        elif self.path == "/api/config":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(get_config()).encode('utf-8'))

        # 4. Unix AI Generator App Route
        elif self.path == "/apps/cmd-gen":
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            if os.path.exists("templates/cmd_gen.html"):
                with open("templates/cmd_gen.html", "rb") as f:
                    self.wfile.write(f.read())

        else:
            self.send_error(404)

    def do_POST(self):
        content_length = int(self.headers.get('Content-Length', 0))
        post_data = self.rfile.read(content_length)
        
        try:
            payload = json.loads(post_data.decode('utf-8'))
        except json.JSONDecodeError:
            payload = {}

        if self.path == "/api/config":
            save_config(payload)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"status": "SUCCESS", "message": "Configuration saved"}).encode('utf-8'))

        elif self.path == "/api/clearance":
            config = get_config()
            msisdn = payload.get("msisdn", "").strip()
            iccid = payload.get("iccid", "").strip()
            imsi = payload.get("imsi", "").strip()
            sub_type = payload.get("sub_type", "prepaid").lower()
            selected_modules = payload.get("selected_modules", ["SOM", "COM", "Billing", "NMS", "ARM", "SM", "ESB"])

            logging.info(f"--- Starting Clearance Request | MSISDN: {msisdn} | ICCID: {iccid} | IMSI: {imsi} | SubType: {sub_type} ---")

            if not re.match(r"^243\d{9}$", msisdn) or not re.match(r"^[a-zA-Z0-9]{20}$", iccid) or not re.match(r"^\d{15}$", imsi):
                logging.error("Validation failed for input payload parameters.")
                self.send_response(400)
                self.end_headers()
                return

            last_digit = msisdn[-1]
            replacements = {
                "msisdn": msisdn,
                "iccid": iccid,
                "imsi": imsi,
                "last_msisdn_digit": last_digit
            }
            results = []

            # 1. SOM, COM, Billing
            for mod in ["SOM", "COM", "Billing"]:
                if mod in selected_modules:
                    cfg = config.get(mod, {})
                    queries = [safe_replace(q, replacements) for q in cfg.get("queries", [])]
                    res = run_mysql_query(mod, cfg, queries)
                    res["module"] = mod
                    results.append(res)

            # 2. NMS
            if "NMS" in selected_modules:
                nms_cfg = config.get("NMS", {})
                q_key = "queries_prepaid" if sub_type == "prepaid" else "queries_postpaid"
                nms_queries = [safe_replace(q, replacements) for q in nms_cfg.get(q_key, [])]
                nms_res = run_mysql_query("NMS", nms_cfg, nms_queries)
                nms_res["module"] = "NMS"
                results.append(nms_res)

            # 3. ARM
            if "ARM" in selected_modules:
                arm_cfg = config.get("ARM", {})
                q_key = "queries_prepaid" if sub_type == "prepaid" else "queries_postpaid"
                arm_queries = [safe_replace(q, replacements) for q in arm_cfg.get(q_key, [])]
                arm_res = run_mysql_query("ARM", arm_cfg, arm_queries)
                arm_res["module"] = "ARM"
                results.append(arm_res)

            # 4. SM
            if "SM" in selected_modules:
                sm_cfg = config.get("SM", {})
                sm_queries = [safe_replace(q, replacements) for q in sm_cfg.get("queries", [])]
                sm_res = run_mysql_query("SM", sm_cfg, sm_queries)
                sm_res["module"] = "SM"
                results.append(sm_res)

            # 5. ESB
            if "ESB" in selected_modules:
                esb_cfg = config.get("ESB", {})
                esb_res = run_esb_curl(esb_cfg.get("curl_command", ""), msisdn)
                esb_res["module"] = "ESB"
                results.append(esb_res)

            logging.info("--- Completed Clearance Request ---\n")

            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"summary": results}).encode('utf-8'))

        elif self.path == "/api/generate-cmd":
            # REUSED parsed 'payload' from top of function
            prompt = payload.get("prompt", "")
            ai_response = query_ollama_qwen(prompt)

            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(ai_response).encode('utf-8'))
            
        else:
            self.send_error(404, "API endpoint not found")

# if __name__ == "__main__":
#     print(f"Server starting on port {PORT}... Logging to {LOG_FILE}")
#     with socketserver.TCPServer(("", PORT), UtilityHandler) as httpd:
#         httpd.serve_forever()

if __name__ == "__main__":
    print(f"Server starting on port {PORT}... Logging to {LOG_FILE}")
    
    # Enable address reuse to prevent WinError 10048 on rapid restarts
    socketserver.TCPServer.allow_reuse_address = True
    
    with socketserver.TCPServer(("0.0.0.0", PORT), UtilityHandler) as httpd:
        httpd.serve_forever()