import os
import sys
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy_cockroachdb import run_transaction
from sqlalchemy.orm.exc import NoResultFound, MultipleResultsFound
from werkzeug.exceptions import HTTPException
import uuid

import requests
from flask import Flask, jsonify

# NOTE: make sure to run this app.py from this folder, so python app.py so that models are also read correctly from root
sys.path.append("../")
from orm_models.models import Order, Payment, User

stock_url = os.environ['STOCK_URL']
order_url = os.environ['ORDER_URL']
datebase_url = os.environ['DATABASE_URL']

app = Flask("payment-service")

# DATABASE_URL= "cockroachdb://root@localhost:26257/defaultdb?sslmode=disable"

# Create engine to connect to the database
try:
    engine = create_engine(datebase_url)
except Exception as e:
    print("Failed to connect to database.")
    print(f"{e}")

# Catch all unhandled exceptions
@app.errorhandler(Exception)
def handle_exception(e):
    # pass through HTTP errors
    if isinstance(e, HTTPException):
        return jsonify(error=str(e)), 400
    
    # now you're handling non-HTTP exceptions only
    return jsonify(error=str(e)), 400

class NotEnoughCreditException(Exception):
    """Exception class for handling insufficient credits of a user"""
    def __str__(self) -> str:
         return "Not enough credits"


@app.post('/create_user')
def create_user():
    user_uuid = uuid.uuid4()
    new_user = User(user_id=user_uuid)
    run_transaction(sessionmaker(bind=engine), lambda s: s.add(new_user))
    return jsonify(user_id=user_uuid), 200

def find_user_helper(session, user_id):
    user = session.query(User).filter(User.user_id == user_id).one()
    return user

@app.get('/find_user/<user_id>')
def find_user(user_id: str):
    try:
        # expire_on_commit=False to reuse returned User object attrs
        ret_user = run_transaction(
            sessionmaker(bind=engine, expire_on_commit=False),
            lambda s: find_user_helper(s, user_id)
        )

        return jsonify(ret_user.to_dict()), 200
    except NoResultFound:
        return "No user was found", 400
    except MultipleResultsFound:
        return "Multiple users were found while one is expected", 400

def add_credit_helper(session, user_id, amount):
    user = session.query(User).filter(User.user_id == user_id).one()
    user.credit += amount

@app.post('/add_funds/<user_id>/<int:amount>')
def add_credit(user_id: str, amount: int):
    try:
        run_transaction(
            sessionmaker(bind=engine),
            lambda s: add_credit_helper(s, user_id, amount)
        )
        return jsonify(done=True), 200
    except NoResultFound:
        print("No user was found")
    except MultipleResultsFound:
        print("Multiple users were found while one is expected")
    return jsonify(done=False), 400

def pay_helper(session, user_id, order_id, amount):
    user = session.query(User).filter(User.user_id == user_id).one()

    order_info = requests.get(f"{order_url}/find/{order_id}").json()
    if not order_info.paid:
        if user.credit >= amount:
            user.credit -= amount
            requests.post(f'{order_url}/pay_order/{user_id}/{order_id}')
            new_payment = Payment(user_id=user_id, order_id=order_id, amount=amount)
            session.add(new_payment)
        else:
            raise NotEnoughCreditException()
        

    
@app.post('/pay/<user_id>/<order_id>/<int:amount>')
def remove_credit(user_id: str, order_id: str, amount: int):
    try:
        run_transaction(
            sessionmaker(bind=engine),
            lambda s: pay_helper(s, user_id, order_id, amount)
        )
        return '', 200
    except NoResultFound:
        return "No user or order was found", 401
    except MultipleResultsFound:
        return "Multiple users or order were found while one is expected", 402
    except NotEnoughCreditException as e:
        return str(e), 403

@app.post('/prepare_pay/<transaction_id>/<user_id>/<order_id>/<int:amount>')
def prepare_remove_credit(transaction_id, user_id: str, order_id: str, amount: int):
    try:
        session = sessionmaker(engine)()
        transactions[transaction_id] = session

        pay_helper(session, user_id, order_id, amount)
        session.flush()
        return 'Ready', 200
    except NoResultFound:
        return "No user or order was found", 401
    except MultipleResultsFound:
        return "Multiple users or order were found while one is expected", 402
    except NotEnoughCreditException as e:
        return str(e), 403

def cancel_payment_helper(session, user_id, order_id):
    user = session.query(User).filter(User.user_id == user_id).one()
    order_info = requests.get(f"{order_url}/find/{order_id}").json()
    payment = session.query(Payment).filter(
        Payment.user_id == user_id,
        Payment.order_id == order_id
    ).one()

    # Only add amount of payment to the user if the order is paid already
    if order_info.paid:
        requests.post(f'{order_url}/cancel_order/{user_id}/{order_id}')
        user.credit += payment.amount
    
    print(session.query(Payment).filter(
        Payment.user_id == user_id,
        Payment.order_id == order_id
    ).delete())
    

@app.post('/cancel/<user_id>/<order_id>')
def cancel_payment(user_id: str, order_id: str):
    try:
        run_transaction(
            sessionmaker(bind=engine), 
            lambda s: cancel_payment_helper(s, user_id, order_id)
        )
        return '', 200
    except NoResultFound:
        return "No user or payment was found", 400
    except MultipleResultsFound:
        return "Multiple users or payments were found while one is expected", 400


def status_helper(session, user_id, order_id):
    payment_paid = session \
        .query(Payment) \
        .filter(
            Payment.user_id == user_id, # and
            Payment.order_id == order_id
        ).first()
    return payment_paid

@app.post('/status/<user_id>/<order_id>')
def payment_status(user_id: str, order_id: str):
    ret_paid = run_transaction(
        sessionmaker(bind=engine, expire_on_commit=False), 
        lambda s: status_helper(s, user_id, order_id)
    )
    if ret_paid:
        return jsonify(paid=True)
    else:
        return jsonify(paid=False)

transactions = {}

@app.post('/endTransaction/<transaction_id>/<status>')
def endTransaction(transaction_id, status):
    try:
        if status == 'commit':
            transactions[transaction_id].commit()
            transactions[transaction_id].close()
        elif status == 'rollback':
            transactions[transaction_id].rollback()
            transactions[transaction_id].close()
        else :
            return 'Unknown status: ' + status, 400
        return 'Success', 200

    except Exception:
        return 'failure', 400

# def main():
#     Base.metadata.create_all(bind=engine, checkfirst=True)
#     app.run(host="0.0.0.0", port=8083, debug=True)

# if __name__ == '__main__':
#     main()