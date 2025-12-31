from pymongo import MongoClient
from pprint import pprint

MONGO_URI = "mongodb+srv://desconexionparcial:LwryVX9pbCjdM8ao@cluster0.7rjoqap.mongodb.net/Registro_Alu"

cliente = MongoClient(MONGO_URI)
db = cliente["Registro_Alu"]

print("📦 Total de asistencias en MongoDB:", db.asistencias.count_documents({}))
print("\n🔹 Ejemplo de registros recientes:")
for doc in db.asistencias.find().sort("fecha", -1).limit(5):
    pprint(doc)
