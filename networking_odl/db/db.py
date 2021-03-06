# Copyright (c) 2015 OpenStack Foundation
# All Rights Reserved.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.
from sqlalchemy import asc
from sqlalchemy import func
from sqlalchemy import or_

from networking_odl.common import constants as odl_const
from networking_odl.db import models

from neutron.db import api as db_api

from oslo_db import api as oslo_db_api


def _check_for_pending_or_processing_ops(session, object_uuid, operation=None):
    q = session.query(models.OpendaylightJournal).filter(
        or_(models.OpendaylightJournal.state == 'pending',
            models.OpendaylightJournal.state == 'processing'),
        models.OpendaylightJournal.object_uuid == object_uuid)
    if operation:
        if isinstance(operation, (list, tuple)):
            q = q.filter(models.OpendaylightJournal.operation.in_(operation))
        else:
            q = q.filter(models.OpendaylightJournal.operation == operation)
    return session.query(q.exists()).scalar()


def _check_for_pending_delete_ops_with_parent(session, object_type,
                                              parent_id):
    rows = session.query(models.OpendaylightJournal).filter(
        or_(models.OpendaylightJournal.state == 'pending',
            models.OpendaylightJournal.state == 'processing'),
        models.OpendaylightJournal.object_type == object_type,
        models.OpendaylightJournal.operation == odl_const.ODL_DELETE
    ).all()

    for row in rows:
        if parent_id in row.data:
            return True

    return False


def _check_for_pending_or_processing_add(session, router_id, subnet_id):
    rows = session.query(models.OpendaylightJournal).filter(
        or_(models.OpendaylightJournal.state == 'pending',
            models.OpendaylightJournal.state == 'processing'),
        models.OpendaylightJournal.object_type == odl_const.ODL_ROUTER_INTF,
        models.OpendaylightJournal.operation == odl_const.ODL_ADD
    ).all()

    for row in rows:
        if router_id in row.data.values() and subnet_id in row.data.values():
            return True

    return False


def _check_for_pending_remove_ops_with_parent(session, parent_id):
    rows = session.query(models.OpendaylightJournal).filter(
        or_(models.OpendaylightJournal.state == 'pending',
            models.OpendaylightJournal.state == 'processing'),
        models.OpendaylightJournal.object_type == odl_const.ODL_ROUTER_INTF,
        models.OpendaylightJournal.operation == odl_const.ODL_REMOVE
    ).all()

    for row in rows:
        if parent_id in row.data.values():
            return True

    return False


def get_all_db_rows(session):
    return session.query(models.OpendaylightJournal).all()


def get_all_db_rows_by_state(session, state):
    return session.query(models.OpendaylightJournal).filter_by(
        state=state).all()


def get_oldest_pending_db_row_with_lock(session):
    row = session.query(models.OpendaylightJournal).filter_by(
        state='pending').order_by(
        asc(models.OpendaylightJournal.last_retried)).with_for_update().first()
    if row:
        update_pending_db_row_processing(session, row)

    return row


@oslo_db_api.wrap_db_retry(max_retries=db_api.MAX_RETRIES,
                           retry_on_request=True)
def update_pending_db_row_processing(session, row):
    row.state = 'processing'
    session.merge(row)
    session.flush()


@oslo_db_api.wrap_db_retry(max_retries=db_api.MAX_RETRIES,
                           retry_on_request=True)
def update_pending_db_row_retry(session, row, retry_count):
    if row.retry_count >= retry_count:
        row.state = 'failed'
    else:
        row.retry_count = row.retry_count + 1
        row.state = 'pending'
    session.merge(row)
    session.flush()


@oslo_db_api.wrap_db_retry(max_retries=db_api.MAX_RETRIES,
                           retry_on_request=True)
def update_processing_db_row_passed(session, row):
    row.state = 'completed'
    session.merge(row)
    session.flush()


@oslo_db_api.wrap_db_retry(max_retries=db_api.MAX_RETRIES,
                           retry_on_request=True)
def update_db_row_pending(session, row):
    row.state = 'pending'
    session.merge(row)
    session.flush()


# This function is currently not used.
# Deleted resources are marked as 'deleted' in the database.
@oslo_db_api.wrap_db_retry(max_retries=db_api.MAX_RETRIES,
                           retry_on_request=True)
def delete_row(session, row=None, row_id=None):
    if row_id:
        row = session.query(models.OpendaylightJournal).filter_by(
            id=row_id).one()
    if row:
        session.delete(row)
        session.flush()


@oslo_db_api.wrap_db_retry(max_retries=db_api.MAX_RETRIES,
                           retry_on_request=True)
def create_pending_row(session, object_type, object_uuid,
                       operation, data):
    row = models.OpendaylightJournal(object_type=object_type,
                                     object_uuid=object_uuid,
                                     operation=operation, data=data,
                                     created_at=func.now(), state='pending')
    session.add(row)
    # Keep session flush for unit tests. NOOP for L2/L3 events since calls are
    # made inside database session transaction with subtransactions=True.
    session.flush()


def validate_network_operation(session, object_uuid, operation, data):
    """Validate the network operation based on dependencies.

    Validate network operation depending on whether it's dependencies
    are still in 'pending' or 'processing' state. e.g.
    """
    if operation == odl_const.ODL_DELETE:
        # Check for any pending or processing create or update
        # ops on this uuid itself
        if _check_for_pending_or_processing_ops(
            session, object_uuid, [odl_const.ODL_UPDATE,
                                   odl_const.ODL_CREATE]):
            return False
        # Check for dependent operations
        if _check_for_pending_delete_ops_with_parent(
            session, odl_const.ODL_SUBNET, object_uuid):
            return False
        if _check_for_pending_delete_ops_with_parent(
            session, odl_const.ODL_PORT, object_uuid):
            return False
        if _check_for_pending_delete_ops_with_parent(
            session, odl_const.ODL_ROUTER, object_uuid):
            return False
    elif operation == odl_const.ODL_UPDATE:
        # Check for a pending create operation on this uuid
        if _check_for_pending_or_processing_ops(
            session, object_uuid, odl_const.ODL_CREATE):
            return False
    return True


def validate_subnet_operation(session, object_uuid, operation, data):
    """Validate the subnet operation based on dependencies.

    Validate subnet operation depending on whether it's dependencies
    are still in 'pending' or 'processing' state. e.g.
    """
    if operation in (odl_const.ODL_CREATE, odl_const.ODL_UPDATE):
        network_id = data['network_id']
        # Check for pending or processing network operations
        if _check_for_pending_or_processing_ops(session, network_id):
            return False
        if operation == odl_const.ODL_UPDATE:
            # Check for a pending or processing create operation on this uuid
            if _check_for_pending_or_processing_ops(
                session, object_uuid, odl_const.ODL_CREATE):
                return False
    elif operation == odl_const.ODL_DELETE:
        # Check for any pending or processing create or update
        # ops on this uuid itself
        if _check_for_pending_or_processing_ops(
            session, object_uuid, [odl_const.ODL_UPDATE,
                                   odl_const.ODL_CREATE]):
            return False
        # Check for dependent operations
        if _check_for_pending_delete_ops_with_parent(
            session, odl_const.ODL_PORT, object_uuid):
            return False

    return True


def validate_port_operation(session, object_uuid, operation, data):
    """Validate port operation based on dependencies.

    Validate port operation depending on whether it's dependencies
    are still in 'pending' or 'processing' state. e.g.
    """
    if operation in (odl_const.ODL_CREATE, odl_const.ODL_UPDATE):
        network_id = data['network_id']
        # Check for pending or processing network operations
        ops = _check_for_pending_or_processing_ops(session, network_id)
        # Check for pending subnet operations.
        for fixed_ip in data['fixed_ips']:
            ip_ops = _check_for_pending_or_processing_ops(
                session, fixed_ip['subnet_id'])
            ops = ops or ip_ops

        if ops:
            return False
        if operation == odl_const.ODL_UPDATE:
            # Check for any pending or processing create or update
            # ops on this uuid itself
            if _check_for_pending_or_processing_ops(
                session, object_uuid, odl_const.ODL_CREATE):
                return False
    elif operation == odl_const.ODL_DELETE:
        # Check for any pending or processing create or update
        # ops on this uuid itself
        if _check_for_pending_or_processing_ops(
            session, object_uuid, [odl_const.ODL_UPDATE,
                                   odl_const.ODL_CREATE]):
            return False

    return True


def validate_router_operation(session, object_uuid, operation, data):
    """Validate router operation based on dependencies.

    Validate router operation depending on whether it's dependencies
    are still in 'pending' or 'processing' state.
    """
    if operation in (odl_const.ODL_CREATE, odl_const.ODL_UPDATE):
        if data['gw_port_id'] is not None:
            if _check_for_pending_or_processing_ops(session,
                                                    data['gw_port_id']):
                return False
        if operation == odl_const.ODL_UPDATE:
            # Check for a pending or processing create operation on this uuid.
            if _check_for_pending_or_processing_ops(
                session, object_uuid, odl_const.ODL_CREATE):
                return False
    elif operation == odl_const.ODL_DELETE:
        # Check for any pending or processing create or update
        # operations on this uuid.
        if _check_for_pending_or_processing_ops(session, object_uuid,
                                                [odl_const.ODL_UPDATE,
                                                 odl_const.ODL_CREATE]):
            return False

        # Check that dependent port delete operation has completed.
        if _check_for_pending_delete_ops_with_parent(
            session, odl_const.ODL_PORT, object_uuid):
            return False

        # Check that dependent floatingip delete operation has completed.
        if _check_for_pending_delete_ops_with_parent(
                session, odl_const.ODL_FLOATINGIP, object_uuid):
            return False

        # Check that dependent router interface remove operation has completed.
        if _check_for_pending_remove_ops_with_parent(session, object_uuid):
            return False

    return True


def validate_floatingip_operation(session, object_uuid, operation, data):
    """Validate floatingip operation based on dependencies.

    Validate floating IP operation depending on whether it's dependencies
    are still in 'pending' or 'processing' state.
    """
    if operation in (odl_const.ODL_CREATE, odl_const.ODL_UPDATE):
        network_id = data.get('floating_network_id')
        if network_id is not None:
            if not _check_for_pending_or_processing_ops(session, network_id):
                port_id = data.get('port_id')
                if port_id is not None:
                    if _check_for_pending_or_processing_ops(session, port_id):
                        return False
            else:
                return False

        router_id = data.get('router_id')
        if router_id is not None:
            if _check_for_pending_or_processing_ops(session, router_id):
                return False

        if operation == odl_const.ODL_UPDATE:
            # Check for a pending or processing create operation on this uuid
            if _check_for_pending_or_processing_ops(
                session, object_uuid, odl_const.ODL_CREATE):
                return False
    elif operation == odl_const.ODL_DELETE:
        # Check for any pending or processing create or update
        # ops on this uuid itself
        if _check_for_pending_or_processing_ops(session, object_uuid,
                                                [odl_const.ODL_UPDATE,
                                                 odl_const.ODL_CREATE]):
            return False

    return True


def validate_router_interface_operation(session, object_uuid, operation, data):
    """Validate router_interface operation based on dependencies.

    Validate router_interface operation depending on whether it's dependencies
    are still in 'pending' or 'processing' state.
    """
    if operation == odl_const.ODL_ADD:
        # Verify that router event has been completed.
        if _check_for_pending_or_processing_ops(session, data['id']):
            return False

        # TODO(rcurran): Check for port_id?
        if _check_for_pending_or_processing_ops(session, data['subnet_id']):
            return False
    elif operation == odl_const.ODL_REMOVE:
        if _check_for_pending_or_processing_add(session, data['id'],
                                                data['subnet_id']):
            return False

    return True


def validate_security_group_operation(session, object_uuid, operation, data):
    """Validate security_group operation based on dependencies.

    Validate security_group operation depending on whether it's dependencies
    are still in 'pending' or 'processing' state. e.g.
    """
    return True


def validate_security_group_rule_operation(session, object_uuid, operation,
                                           data):
    """Validate security_group_rule operation based on dependencies.

    Validate security_group_rule operation depending on whether it's
    dependencies are still in 'pending' or 'processing' state. e.g.
    """
    return True

VALIDATION_MAP = {
    odl_const.ODL_NETWORK: validate_network_operation,
    odl_const.ODL_SUBNET: validate_subnet_operation,
    odl_const.ODL_PORT: validate_port_operation,
    odl_const.ODL_ROUTER: validate_router_operation,
    odl_const.ODL_ROUTER_INTF: validate_router_interface_operation,
    odl_const.ODL_FLOATINGIP: validate_floatingip_operation,
    odl_const.ODL_SG: validate_security_group_operation,
    odl_const.ODL_SG_RULE: validate_security_group_rule_operation,
}
