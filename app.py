from flask import Flask
from flask_cors import CORS
from routes.macros_routes import macros_bp
from routes.mealplan_routes import mealplan_bp
from routes.checkout_summary import checkout_bp
from routes.confirm_order import confirm_order_bp
from routes.partners_routes import partner_bp
from routes.cooking import cooking_bp
from routes.ingredients import ingredients_bp
from routes.portioning import portioning_bp
from routes.packaging import packaging_bp
from routes.client_meals import client_meals_bp
from routes.price_simulator import simple_price_bp  
from routes.get_available_recipes import get_available_recipes_bp
import os

app = Flask(__name__)
CORS(app, resources={r"/*": {"origins": "*"}})   # <--- FIX

# Register blueprints
app.register_blueprint(macros_bp)
app.register_blueprint(confirm_order_bp)
app.register_blueprint(mealplan_bp)
app.register_blueprint(checkout_bp)
app.register_blueprint(partner_bp)
app.register_blueprint(ingredients_bp)
app.register_blueprint(cooking_bp)
app.register_blueprint(portioning_bp)
app.register_blueprint(packaging_bp)
app.register_blueprint(client_meals_bp)
app.register_blueprint(simple_price_bp)  # <--- FIX
app.register_blueprint(get_available_recipes_bp)

@app.route("/")
def home():
    return "Hello from Flask API on Heroku!!"

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
