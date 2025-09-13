from flask import Flask, render_template, request, jsonify
import base64
import gzip
import json
import re
from typing import Any, Dict, Optional

app = Flask(__name__)

class BlueprintConverter:
    """A class to handle the decoding, modification, and encoding of Shapez.io blueprints."""

    def __init__(self, game_string: str = ""):
        self.game_string = game_string
        self.decoded_bp: Optional[dict[str, Any]] = None

    def decode_blueprint(self) -> bool:
        """Decodes the Shapez save string."""
        prefix = "SHAPEZ2-3-"
        suffix = "$"
        if not (self.game_string.startswith(prefix) and self.game_string.endswith(suffix)):
            return False
        
        payload = self.game_string[len(prefix):-len(suffix)]
        payload = payload.strip()
        if (pad := (-len(payload) % 4)) != 0:
            payload += "=" * pad
        
        try:
            decoded_bytes = base64.b64decode(payload, validate=False)
            decompressed = gzip.decompress(decoded_bytes)
            self.decoded_bp = json.loads(decompressed.decode("utf-8"))
            return True
        except Exception:
            return False

    def decode_c_fields(self) -> None:
        """Recursively decodes 'C' fields into 'C-decoded' based on the entry type."""
        if not self.decoded_bp:
            return

        def process_node(node: Any):
            if isinstance(node, dict):
                if "C" in node and isinstance(node["C"], str):
                    b64 = node["C"].strip()
                    if (pad := (-len(b64) % 4)) != 0:
                        b64 += "=" * pad
                    try:
                        raw = base64.b64decode(b64, validate=False)
                        
                        # Handle ButtonDefaultInternalVariant with specific 'on'/'off' states
                        if node.get("T") == "ButtonDefaultInternalVariant":
                            if raw == b'\x01':
                                node["C-decoded"] = "on"
                            elif raw == b'\x00':
                                node["C-decoded"] = "off"
                            else:
                                node["C-decoded"] = f"[unknown button state: {raw.hex()}]"
                        # Handle LogicGateCompareInternalVariant with a specific mapping
                        elif node.get("T") == "LogicGateCompareInternalVariant" and len(raw) == 1:
                            # Map the single byte to the correct operator string
                            op_map = {
                                1: "==", 2: ">=", 3: ">", 4: "<", 5: "<=", 6: "!="
                            }
                            op_value = int.from_bytes(raw, 'little')
                            node["C-decoded"] = op_map.get(op_value, f"[unknown op: {op_value}]")

                        # Handle labels
                        elif node.get("T") == "LabelDefaultInternalVariant":
                            if len(raw) >= 2 and raw[1] == 0x00:
                                string_part = raw[2:].decode("utf-8")
                                node["C-decoded"] = string_part
                            else:
                                node["C-decoded"] = f"[decoding error: unexpected label format]"
                        # Handle other constant signals
                        else:
                            # Check for special single-byte values first
                            if raw == b'\x00':
                                node["C-decoded"] = "empty"
                            elif raw == b'\x01':
                                node["C-decoded"] = "null"
                            elif raw == b'\x02':
                                node["C-decoded"] = "conflict"
                            # Number decoding (type 0x03, 4 data bytes)
                            elif len(raw) == 5 and raw[0] == 0x03:
                                num_value = int.from_bytes(raw[1:], 'little', signed=True)
                                node["C-decoded"] = str(num_value)
                            # Color decoding (type 0x07, length 0x01, 1 data byte)
                            elif len(raw) == 3 and raw[0] == 0x07 and raw[1] == 0x01:
                                node["C-decoded"] = chr(raw[2])
                            # Generic shape/signal decoding (dynamic length prefix)
                            elif raw.startswith(b'\x06\x01\x01'):
                                if len(raw) > 4:
                                    string_len = raw[3]
                                    if len(raw) >= 5 + string_len:
                                        string_part = raw[5:5 + string_len].decode("utf-8")
                                        node["C-decoded"] = string_part
                                    else:
                                        node["C-decoded"] = f"[decoding error: raw length {len(raw)} vs expected {5 + string_len}]"
                                else:
                                    node["C-decoded"] = f"[decoding error: missing length byte in prefix]"
                            else:
                                string_part = raw.decode("utf-8", errors="ignore")
                                node["C-decoded"] = string_part
                    except Exception as exc:
                        node["C-decoded"] = f"[base64 error: {exc}]"
                
                for k, v in node.items():
                    process_node(v)
            elif isinstance(node, list):
                for item in node:
                    process_node(item)
        
        process_node(self.decoded_bp)

    def encode_c_fields(self) -> None:
        """Recursively re-encodes 'C-decoded' keys back to 'C'."""
        if not self.decoded_bp:
            return

        def process_node(node: Any):
            if isinstance(node, dict):
                if "C-decoded" in node:
                    decoded_value = node["C-decoded"]
                    new_c_value = None
                    
                    # Handle ButtonDefaultInternalVariant
                    if node.get("T") == "ButtonDefaultInternalVariant":
                        if decoded_value == "on":
                            new_c_value = base64.b64encode(b'\x01').decode("utf-8")
                        elif decoded_value == "off":
                            new_c_value = base64.b64encode(b'\x00').decode("utf-8")
                    # Handle LogicGateCompareInternalVariant first
                    elif node.get("T") == "LogicGateCompareInternalVariant":
                        # Map the operator string to the correct byte
                        op_map = {
                            "==": 1, ">=": 2, ">": 3, "<": 4, "<=": 5, "!=": 6
                        }
                        op_byte = op_map.get(decoded_value)
                        if op_byte is not None:
                            raw_bytes = op_byte.to_bytes(1, 'little')
                            new_c_value = base64.b64encode(raw_bytes).decode("utf-8")
                        else:
                            # Keep the C field as is, if not a known operator
                            pass
                    # Handle labels
                    elif node.get("T") == "LabelDefaultInternalVariant":
                        # Always encode as a string with a dynamic length prefix
                        string_bytes = decoded_value.encode("utf-8")
                        string_len = len(string_bytes)
                        # A sensible default prefix for a new label string
                        prefix = string_len.to_bytes(1, 'little') + b'\x00'
                        raw_bytes = prefix + string_bytes
                        new_c_value = base64.b64encode(raw_bytes).decode("utf-8")
                    # Handle other constant signals
                    elif decoded_value == "empty":
                        new_c_value = base64.b64encode(b'\x00').decode("utf-8")
                    elif decoded_value == "null":
                        new_c_value = base64.b64encode(b'\x01').decode("utf-8")
                    elif decoded_value == "conflict":
                        new_c_value = base64.b64encode(b'\x02').decode("utf-8")
                    else:
                        try:
                            # Try to encode as a number
                            num_value = int(decoded_value)
                            raw_bytes = bytearray([0x03]) + num_value.to_bytes(4, 'little', signed=True)
                            new_c_value = base64.b64encode(raw_bytes).decode("utf-8")
                        except (ValueError, TypeError):
                            # Try to encode as a single-character color string
                            if isinstance(decoded_value, str) and len(decoded_value) == 1 and decoded_value in "rgbwycmu":
                                raw_bytes = bytearray([0x07, 0x01, ord(decoded_value)])
                                new_c_value = base64.b64encode(raw_bytes).decode("utf-8")
                            # If all else fails, assume it's a shape/signal string
                            else:
                                string_bytes = decoded_value.encode("utf-8")
                                string_len = len(string_bytes)
                                # Dynamically create the shape prefix with the correct length byte
                                raw_bytes = b'\x06\x01\x01' + string_len.to_bytes(1, 'little') + b'\x00' + string_bytes
                                new_c_value = base64.b64encode(raw_bytes).decode("utf-8")
                            
                    if new_c_value:
                        node["C"] = new_c_value
                        del node["C-decoded"]
                
                for k, v in list(node.items()):
                    process_node(v)
            elif isinstance(node, list):
                for item in node:
                    process_node(item)

        process_node(self.decoded_bp)

    def encode_blueprint(self) -> Optional[str]:
        """Encodes the modified blueprint back into a Shapez save string."""
        if not self.decoded_bp:
            return None
        
        try:
            json_bytes = json.dumps(self.decoded_bp, separators=(',', ':'), sort_keys=True).encode("utf-8")
            compressed_bytes = gzip.compress(json_bytes)
            base64_payload = base64.b64encode(compressed_bytes).decode("utf-8")
            encoded_string = f"SHAPEZ2-3-{base64_payload}$"
            return encoded_string
        except Exception:
            return None

@app.route('/', methods=['GET', 'POST'])
def index():
    game_string = ""
    json_output = ""
    status = "Ready."
    decoded_data = None

    if request.method == 'POST':
        game_string = request.form.get('game_string', '').strip()
        json_input = request.form.get('json_output', '')

        converter = BlueprintConverter(game_string)

        # Prioritize JSON input if it's provided
        if json_input:
            try:
                converter.decoded_bp = json.loads(json_input)
                decoded_data = converter.decoded_bp
                converter.encode_c_fields() # call the encode to avoid conflicts between c and c-decoded values
                new_game_string = converter.encode_blueprint()
                if new_game_string:
                    game_string = new_game_string
                    json_output = json_input
                    status = "Encoding successful!"
                else:
                    status = "Encoding failed: Check the JSON structure."
                    json_output = json_input

            except json.JSONDecodeError as e:
                status = f"Encoding failed: Invalid JSON format: {e}"
                json_output = json_input


        # Otherwise, try decoding from the game string
        elif game_string:
            if not converter.decode_blueprint():
                status = "Decoding failed: Invalid blueprint string."
            else:
                converter.decode_c_fields()
                decoded_data = converter.decoded_bp
                json_output = json.dumps(decoded_data, indent=2)
                status = "Decoding successful!"
        else:
            status = "Ready."


    return render_template('index.html', game_string=game_string, json_output=json_output, status=status, decoded_data=decoded_data)

if __name__ == '__main__':
    app.run(debug=True)