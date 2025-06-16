import boto3

# --- KONFIGURASI ---
TABLE_NAME = 'dataSensor'
AWS_REGION = 'ap-southeast-1' # Ganti dengan region Anda jika berbeda
# -------------------

# Pastikan AWS credentials Anda sudah terkonfigurasi (misal via 'aws configure')
dynamodb = boto3.resource('dynamodb', region_name=AWS_REGION)
table = dynamodb.Table(TABLE_NAME)

print(f"Memulai proses pengosongan tabel: {TABLE_NAME}...")

# Langkah 1: Scan tabel untuk mendapatkan semua primary key.
# Untuk tabel besar, proses scan akan dipaginasi.
scan = table.scan(
    ProjectionExpression='#pk, #sk', # Hanya ambil primary key untuk efisiensi
    ExpressionAttributeNames={
        '#pk': 'deviceId', # Ganti 'deviceId' dengan nama partition key Anda
        '#sk': 'timestamp' # Ganti 'timestamp' dengan nama sort key Anda
    }
)

items_to_delete = scan.get('Items', [])
while 'LastEvaluatedKey' in scan:
    print(f"Mengambil {len(items_to_delete)} item...")
    scan = table.scan(
        ProjectionExpression='#pk, #sk',
        ExpressionAttributeNames={
            '#pk': 'deviceId',
            '#sk': 'timestamp'
        },
        ExclusiveStartKey=scan['LastEvaluatedKey']
    )
    items_to_delete.extend(scan.get('Items', []))

if not items_to_delete:
    print("Tabel sudah kosong. Tidak ada yang perlu dihapus.")
    exit()

print(f"Total item yang akan dihapus: {len(items_to_delete)}")

# Langkah 2: Hapus item dalam batch untuk efisiensi
# DynamoDB hanya memperbolehkan maksimal 25 item per batch write.
with table.batch_writer() as batch:
    for i, item in enumerate(items_to_delete):
        print(f"Menghapus item {i+1}/{len(items_to_delete)} -> {item}")
        batch.delete_item(Key=item)

print(f"\nProses selesai. Tabel {TABLE_NAME} telah dikosongkan.")