"""Strip transport credentials; preserve source data, including raw bytes."""
import base64

def clean(value):
    if isinstance(value,bytes):return {'encoding':'base64','data':base64.b64encode(value).decode()}
    if isinstance(value,dict):
        return {k:clean(v) for k,v in value.items() if str(k).lower() not in
                {'token','accesstoken','access_token','cookie','cookies','authorization','password','secret','sessionwebhook'}}
    if isinstance(value,list):return [clean(v) for v in value]
    return value
