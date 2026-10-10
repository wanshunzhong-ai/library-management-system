"""读者 REST 接口。"""

from __future__ import annotations

from flask import Blueprint, jsonify, request

from library.members import service

bp = Blueprint("members", __name__, url_prefix="/api/members")


@bp.get("")
def list_members():
    return jsonify(service.list_members(request.args.to_dict(flat=True)))


@bp.get("/<int:member_id>")
def get_member(member_id: int):
    return jsonify(service.get_member(member_id))


@bp.post("")
def create_member():
    return jsonify(service.create_member(request.get_json(silent=True) or {})), 201


@bp.put("/<int:member_id>")
def update_member(member_id: int):
    return jsonify(service.update_member(member_id, request.get_json(silent=True) or {}))


@bp.delete("/<int:member_id>")
def delete_member(member_id: int):
    service.delete_member(member_id)
    return jsonify({"deleted": member_id})
