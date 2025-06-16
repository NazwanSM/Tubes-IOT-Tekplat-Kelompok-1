from flask import Flask, request, jsonify
from datetime import datetime, timedelta
import boto3 # Untuk DynamoDB
from boto3.dynamodb.conditions import Key # Untuk query DynamoDB
import os
import google.generativeai as genai
from dotenv import load_dotenv
from flask_cors import CORS
import time # Untuk timestamp epoch
from flask import render_template

load_dotenv()

app = Flask(__name__)
CORS(app)

# --- Konfigurasi AWS DynamoDB ---
# Pastikan AWS credentials Anda terkonfigurasi (misalnya via `aws configure` atau environment variables)
# atau IAM role jika berjalan di EC2/Lambda.
DYNAMODB_REGION = 'ap-southeast-1' # Ganti jika region Anda berbeda
SENSOR_DATA_TABLE_NAME = 'dataSensor' # Nama tabel data sensor Anda
SYSTEM_ALERTS_TABLE_NAME = 'SystemAlerts' # Nama tabel untuk status alert (buat jika belum ada)

try:
    dynamodb = boto3.resource('dynamodb', region_name=DYNAMODB_REGION)
    sensor_table = dynamodb.Table(SENSOR_DATA_TABLE_NAME)
    alerts_table = dynamodb.Table(SYSTEM_ALERTS_TABLE_NAME)
    print(f"Berhasil terhubung ke DynamoDB table: {SENSOR_DATA_TABLE_NAME} dan {SYSTEM_ALERTS_TABLE_NAME}")
except Exception as e:
    print(f"ERROR: Tidak dapat terhubung ke DynamoDB: {e}")
    # Anda mungkin ingin menghentikan aplikasi di sini jika koneksi DB gagal saat startup
    dynamodb = None
    sensor_table = None
    alerts_table = None


@app.route('/')
def home():
    return render_template('hello.html')

# --- Konfigurasi Gemini AI ---
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
if not GEMINI_API_KEY:
    print("WARNING: GEMINI_API_KEY not found in .env file. Gemini AI features will not work.")
    gemini_model = None
else:
    genai.configure(api_key=GEMINI_API_KEY)
    gemini_model = genai.GenerativeModel('gemini-1.5-flash-latest')

# --- Logika Alert Persisten ---
ALERT_DURATION_SECONDS = 5 * 60 # 5 menit (300 detik)
DEFAULT_ALERT_ID = "global_alert" # ID item untuk status alert di tabel SystemAlerts

def update_persistent_alert_status(is_active, reason, start_time_epoch=None):
    if not alerts_table:
        print("ERROR: Alerts table not initialized.")
        return

    current_time_iso = datetime.now().isoformat()
    item_to_update = {
        'alertId': DEFAULT_ALERT_ID, # Kunci Partisi
        'isActive': is_active,
        'reason': reason,
        'lastUpdate': current_time_iso
    }
    if start_time_epoch is not None:
        item_to_update['startTimeEpoch'] = int(start_time_epoch)
    
    try:
        # Jika startTimeEpoch tidak disediakan, kita tidak ingin menghapusnya jika sudah ada
        # Jadi kita perlu get lalu put, atau menggunakan update_item dengan lebih hati-hati
        # Untuk kesederhanaan, kita get dulu jika startTimeEpoch adalah None saat update
        
        if start_time_epoch is None:
            existing_status = get_persistent_alert_status()
            if existing_status and 'startTimeEpoch' in existing_status and existing_status['startTimeEpoch'] is not None:
                item_to_update['startTimeEpoch'] = existing_status['startTimeEpoch'] # Pertahankan start time lama

        alerts_table.put_item(Item=item_to_update)
        print(f"Persistent alert status updated: active={is_active}, reason={reason}")
    except Exception as e:
        print(f"ERROR: Failed to update persistent alert status in DynamoDB: {e}")


def get_persistent_alert_status():
    if not alerts_table:
        print("ERROR: Alerts table not initialized.")
        return {"alert_active": False, "reason": "Sistem normal (DB error).", "start_time_epoch": None}

    status = {"alert_active": False, "reason": "Sistem normal.", "start_time_epoch": None}
    try:
        response = alerts_table.get_item(Key={'alertId': DEFAULT_ALERT_ID})
        if 'Item' in response:
            item = response['Item']
            status["alert_active"] = item.get('isActive', False)
            status["reason"] = item.get('reason', "Sistem normal.")
            status["start_time_epoch"] = item.get('startTimeEpoch') # Akan None jika tidak ada
        return status
    except Exception as e:
        print(f"ERROR: Failed to retrieve persistent alert status from DynamoDB: {e}")
        status["reason"] = "Error mengambil status alert."
        return status

# --- Endpoint untuk menerima data dari ESP32 ---
@app.route('/api/sensor_data', methods=['POST'])
def receive_sensor_data():
    if not request.is_json:
        return jsonify({"error": "Request must be JSON"}), 400

    data_from_esp = request.get_json()
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Received data: {data_from_esp}")

    if not sensor_table:
        return jsonify({"error": "Sensor data table not initialized"}), 500

    # **PASTIKAN** data_from_esp memiliki field 'deviceId' dan 'timestamp' (Number)
    # dan field lainnya sesuai dengan yang Anda inginkan di DynamoDB.
    # Contoh: deviceId, timestamp, temperatureC, humidityRH, airQualityRaw, lightLux, dll.
    # Kunci dalam data_from_esp harus sesuai dengan atribut yang ingin Anda simpan.
    
    # Validasi dasar
    if 'deviceId' not in data_from_esp or 'timestamp' not in data_from_esp:
        return jsonify({"error": "Missing 'deviceId' or 'timestamp' in request"}), 400
    if not isinstance(data_from_esp['timestamp'], (int, float)):
         return jsonify({"error": "'timestamp' must be a number"}), 400

    try:
        # Semua field dari data_from_esp akan disimpan sebagai atribut di item DynamoDB
        sensor_table.put_item(Item=data_from_esp)

        # --- Logika Deteksi Alert Persisten (disesuaikan untuk DynamoDB) ---
        # Asumsi field `statusKesimpulan` dan `statusKondisiBahaya` ada di data_from_esp
        current_conclusion = data_from_esp.get('statusKesimpulan', '') # Sesuaikan nama field jika berbeda
        is_bad_state_now = data_from_esp.get('statusKondisiBahaya', False) # Sesuaikan nama field jika berbeda
        # Atau gunakan logika seperti sebelumnya jika 'statusKondisiBahaya' tidak ada:
        # is_bad_state_now = "BAHAYA!" in current_conclusion or "PERINGATAN!" in current_conclusion

        alert_status_db = get_persistent_alert_status()
        
        current_epoch_time = int(time.time())

        if is_bad_state_now:
            if not alert_status_db.get("start_time_epoch"): # Baru mulai kondisi buruk
                update_persistent_alert_status(False, "Kondisi lingkungan buruk terdeteksi, sedang dalam pemantauan.", current_epoch_time)
            elif alert_status_db.get("start_time_epoch") and \
                (current_epoch_time - alert_status_db["start_time_epoch"]) >= ALERT_DURATION_SECONDS:
                if not alert_status_db["alert_active"]: # Aktifkan alert jika belum aktif
                    reason_text = data_from_esp.get('statusKesimpulan', "Kondisi lingkungan buruk terdeteksi.")
                    update_persistent_alert_status(True, f"Kondisi lingkungan terpantau buruk secara konsisten: {reason_text}", alert_status_db["start_time_epoch"])
        else: # Kondisi tidak buruk sekarang
            if alert_status_db["alert_active"] or alert_status_db.get("start_time_epoch"): # Jika sebelumnya aktif atau sedang dipantau
                update_persistent_alert_status(False, "Kondisi lingkungan kembali normal.") # startTimeEpoch akan di-reset (dihapus) jika tidak dipertahankan

        return jsonify({"message": "Data received and stored successfully in DynamoDB"}), 200

    except Exception as e:
        print(f"ERROR: Failed to store data in DynamoDB: {e}")
        return jsonify({"error": f"Failed to store data in DynamoDB: {e}"}), 500

# --- Endpoint untuk mendapatkan data sensor terbaru (untuk frontend) ---
# GANTI FUNGSI LAMA DENGAN VERSI FINAL INI DI DALAM app.py

# Helper function untuk konversi aman ke float
def to_float(value):
    if value is None:
        return None
    try:
        return float(value)
    except (ValueError, TypeError):
        return None

@app.route('/api/latest-data', methods=['GET'])
def get_latest_sensor_data():
    if not sensor_table:
        return jsonify({"error": "Sensor data table not initialized"}), 500

    target_device_id = request.args.get('deviceId', 'esp32-Tubes')

    try:
        response = sensor_table.query(
            KeyConditionExpression=Key('deviceId').eq(target_device_id),
            ScanIndexForward=False,
            Limit=1
        )
        items = response.get('Items', [])
        
        if items:
            latest_data_from_db = items[0]
            
            payload = latest_data_from_db.get('payload', {})
            
            # --- PERBAIKAN FINAL: Konversi semua nilai numerik ke float/int ---
            # Ini memastikan backend mengirim tipe data yang benar (angka, bukan string)
            
            # Data dari luar payload
            is_bad_state_now = latest_data_from_db.get("is_bad_state_now", False)
            alert_reason = latest_data_from_db.get("alert_reason", "Sistem normal.")

            # Data dari dalam payload
            formatted_data_for_frontend = {
                # Data yang sudah ada di luar payload
                "deviceId": latest_data_from_db.get('deviceId'),
                "timestamp": to_float(latest_data_from_db.get('timestamp')),
                "is_bad_state_now": is_bad_state_now,
                "alert_reason": alert_reason,

                # Mengambil dari 'payload' dan memastikan tipenya adalah ANGKA
                "temperatureC": to_float(payload.get("temperatureC")),
                "humidityRH": to_float(payload.get("humidityRH")),
                "airQualityRaw": to_float(payload.get("airQualityRaw")),
                "lightLux": to_float(payload.get("lightLux")),
                "totalAccel": to_float(payload.get("totalAccel")),
                
                # Status string biarkan sebagai string
                "statusDHT": payload.get("statusDHT", "N/A"),
                "statusMQ": payload.get("statusMQ", "N/A"),
                "statusLux": payload.get("statusLux", "N/A"),
                "statusAccel": payload.get("statusAccel", "N/A"),
                "statusKesimpulan": payload.get("statusKesimpulan", "N/A"),
                "statusKondisiBahaya": payload.get("statusKondisiBahaya", False),
            }

            return jsonify(formatted_data_for_frontend), 200
        else:
            return jsonify({"message": f"No data available for deviceId: {target_device_id}"}), 404
    except Exception as e:
        print(f"ERROR: Gagal memproses data dari DynamoDB: {e}")
        return jsonify({"error": f"Gagal memproses data dari DynamoDB: {e}"}), 500

# --- Endpoint untuk memeriksa status alert persisten (untuk pop-up frontend) ---
@app.route('/api/check-alert', methods=['GET'])
def check_alert_status_endpoint(): # Ubah nama fungsi agar unik
    status = get_persistent_alert_status()
    # Frontend mungkin mengharapkan 'alert_active', bukan 'isActive'
    return jsonify({
        "alert_active": status.get('alert_active', False),
        "reason": status.get('reason', "Sistem normal."),
        "start_time_epoch": status.get('start_time_epoch')
    }), 200


# --- Endpoint untuk Gemini AI Recommendation ---
@app.route('/api/gemini-recommendation', methods=['POST'])
def gemini_recommendation():
    if not gemini_model: # Cek jika model tidak terinisialisasi
        return jsonify({"error": "Gemini AI not configured or API Key missing on backend."}), 500

    if not request.is_json:
        return jsonify({"error": "Request must be JSON"}), 400

    sensor_data_from_frontend = request.get_json() # Data ini dikirim dari frontend
    
    # Buat prompt untuk Gemini AI
    # Pastikan field di sini sesuai dengan apa yang dikirim frontend / ada di sensor_data_from_frontend
    prompt = f"""
    Peran Anda adalah sebagai **EcoGuard**, seorang penasihat kesehatan dan keselamatan lingkungan pribadi. Anda ramah, peduli, dan proaktif.

    Analisis data sensor berikut dari sudut pandang **dampaknya bagi kesehatan dan kenyamanan pengguna**:
    - Kondisi Utama: {sensor_data_from_frontend.get('statusKesimpulan', 'N/A')}
    - Suhu: {sensor_data_from_frontend.get('temperatureC', 'N/A')} °C
    - Kelembapan: {sensor_data_from_frontend.get('humidityRH', 'N/A')} %
    - Kualitas Udara (Raw): {sensor_data_from_frontend.get('airQualityRaw', 'N/A')}
    - Gerakan Terdeteksi: {sensor_data_from_frontend.get('statusAccel', 'N/A')}
    - Status Peringatan: {'Aktif' if sensor_data_from_frontend.get('is_bad_state_now') else 'Tidak Aktif'}

    Tujuan Anda:
    Berikan respons yang membuat pengguna merasa diperhatikan.
    Format Jawaban:
    1.  **Sapaan & Status**: Awali dengan sapaan singkat dan ringkasan kondisi (misal: "Halo! Kondisi ruangan Anda saat ini cukup nyaman,").
    2.  **Analisis Dampak**: Jelaskan secara singkat apa arti kondisi tersebut (misal: "...namun kelembapan yang sedikit tinggi bisa membuat gerah.").
    3.  **REKOMENDASI AKSI UTAMA**: Berikan **satu langkah paling jelas dan praktis** yang bisa langsung dilakukan pengguna dalam format poin. Awali dengan emoji yang sesuai.

    Contoh: "Halo! Kondisi ruangan Anda terpantau baik. Untuk menjaganya tetap segar, coba buka ventilasi selama 15 menit."
    Contoh jika bahaya: "Perhatian! Terdeteksi guncangan pada perangkat. ⚠️ **Prioritas utama: segera periksa kondisi perangkat dan lingkungan sekitar Anda untuk memastikan keamanan.**"
    """

    try:
        response = gemini_model.generate_content(prompt)
        recommendation = response.text
        return jsonify({"recommendation": recommendation}), 200
    except Exception as e:
        print(f"ERROR: Gemini AI API call failed: {e}")
        return jsonify({"error": f"Failed to get recommendation from AI: {e}"}), 500

if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0')