from pymongo import MongoClient

uri = "mongodb+srv://desconexionparcial:LwryVX9pbCjdM8ao@cluster0.7rjoqap.mongodb.net/Registro_Alu"
try:
    client = MongoClient(uri, serverSelectionTimeoutMS=5000)
    client.admin.command('ping')
    print("✅ Conexión exitosa a MongoDB Atlas")
except Exception as e:
    print("❌ Error:", e)
