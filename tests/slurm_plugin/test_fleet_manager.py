# Copyright 2020 Amazon.com, Inc. or its affiliates. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License"). You may not use this file except in compliance with
# the License. A copy of the License is located at
#
# http://aws.amazon.com/apache2.0/
#
# or in the "LICENSE.txt" file accompanying this file. This file is distributed on an "AS IS" BASIS, WITHOUT WARRANTIES
# OR CONDITIONS OF ANY KIND, express or implied. See the License for the specific language governing permissions and
# limitations under the License.
import json
import logging
import os
from datetime import datetime, timezone

import pytest
from assertpy import assert_that
from botocore.exceptions import ClientError
from slurm_plugin.fleet_manager import Ec2CreateFleetManager, Ec2RunInstancesManager, FleetManagerFactory

from tests.common import FLEET_CONFIG, MockedBoto3Request


@pytest.fixture()
def boto3_stubber_path():
    # we need to set the region in the environment because the Boto3ClientFactory requires it.
    os.environ["AWS_DEFAULT_REGION"] = "us-east-2"
    return "slurm_plugin.fleet_manager.boto3"


class TestFleetManagerFactory:
    @pytest.mark.parametrize(
        ("fleet_config", "expected_failure", "expected_manager"),
        [
            ({}, "Unable to find queue .* or compute resource .* in the fleet config: {}", None),
            ({"bad_queue": {}}, "Unable to find queue .* or compute resource .* in the .*", None),
            ({"q1": {"bad_cr": {}}}, "Unable to find queue .* or compute resource .* in the .*", None),
            ({"q1": {"cr1": {}}}, "Unable to find 'Api' key in the compute resource 'cr1', .* fleet config: .*", None),
            ({"q1": {"cr1": {"Api": "wrong"}}}, "Unsupported Api 'wrong' specified in queue .*", None),
            ({"q1": {"cr1": {"Api": "run-instances"}, "other": {"Api": "create-fleet"}}}, None, Ec2RunInstancesManager),
            ({"q1": {"cr1": {"Api": "create-fleet"}, "other": {"Api": "run-instances"}}}, None, Ec2CreateFleetManager),
        ],
        ids=[
            "empty_config",
            "missing_queue_in_config",
            "missing_cr_in_config",
            "missing_api_in_config",
            "unsupported_api_in_config",
            "right_config_run_instances",
            "right_config_create_fleet",
        ],
    )
    def test_get_manager(self, fleet_config, expected_failure, expected_manager):
        if expected_failure:
            with pytest.raises(Exception, match=expected_failure):
                FleetManagerFactory.get_manager(
                    "cluster_name", "region", "boto3_config", fleet_config, "q1", "cr1", False, {}, {}
                )
        else:
            manager = FleetManagerFactory.get_manager(
                "cluster_name", "region", "boto3_config", fleet_config, "q1", "cr1", False, {}, {}
            )
            assert_that(manager).is_instance_of(expected_manager)


# -------- Ec2RunInstancesManager ------


class TestEc2RunInstancesManager:
    @pytest.mark.parametrize(
        (
            "batch_size",
            "compute_resource",
            "all_or_nothing",
            "launch_overrides",
            "expected_params",
        ),
        [
            (
                5,
                "p4d24xlarge",
                False,
                {},
                {
                    "MinCount": 1,
                    "MaxCount": 5,
                    "LaunchTemplate": {
                        "LaunchTemplateName": "hit-queue1-p4d24xlarge",
                        "Version": "$Latest",
                    },
                },
            ),
            (
                5,
                "c5xlarge",
                True,
                {},
                {
                    "MinCount": 5,
                    "MaxCount": 5,
                    "LaunchTemplate": {
                        "LaunchTemplateName": "hit-queue1-c5xlarge",
                        "Version": "$Latest",
                    },
                },
            ),
            (
                5,
                "p4d24xlarge",
                False,
                {
                    "queue1": {
                        "p4d24xlarge": {
                            "CapacityReservationSpecification": {
                                "CapacityReservationTarget": {"CapacityReservationId": "cr-12345"}
                            }
                        }
                    }
                },
                {
                    "MinCount": 1,
                    "MaxCount": 5,
                    "LaunchTemplate": {
                        "LaunchTemplateName": "hit-queue1-p4d24xlarge",
                        "Version": "$Latest",
                    },
                    "CapacityReservationSpecification": {
                        "CapacityReservationTarget": {"CapacityReservationId": "cr-12345"}
                    },
                },
            ),
        ],
        ids=["normal", "all_or_nothing_batch", "launch_overrides"],
    )
    def test_evaluate_launch_params(
        self,
        batch_size,
        compute_resource,
        all_or_nothing,
        launch_overrides,
        expected_params,
        caplog,
    ):
        caplog.set_level(logging.INFO)
        # run test
        fleet_manager = FleetManagerFactory.get_manager(
            "hit",
            "region",
            "boto3_config",
            FLEET_CONFIG,
            "queue1",
            compute_resource,
            all_or_nothing,
            launch_overrides,
            {},
        )
        launch_params = fleet_manager._evaluate_launch_params(batch_size)
        if launch_overrides:
            assert_that(caplog.text).contains("Found RunInstances parameters override")
        assert_that(launch_params).is_equal_to(expected_params)

    @pytest.mark.parametrize(
        ("launch_params", "mocked_boto3_request", "expected_assigned_nodes"),
        [
            (
                {
                    "MinCount": 1,
                    "MaxCount": 5,
                    "LaunchTemplate": {
                        "LaunchTemplateName": "hit-queue1-p4d24xlarge",
                        "Version": "$Latest",
                    },
                },
                [
                    MockedBoto3Request(
                        method="run_instances",
                        response={
                            "Instances": [
                                {
                                    "InstanceId": "i-12345",
                                    "PrivateIpAddress": "ip-2",
                                    "PrivateDnsName": "hostname",
                                    "LaunchTime": datetime(2020, 1, 1, tzinfo=timezone.utc),
                                }
                            ]
                        },
                        expected_params={
                            "MinCount": 1,
                            "MaxCount": 5,
                            "LaunchTemplate": {
                                "LaunchTemplateName": "hit-queue1-p4d24xlarge",
                                "Version": "$Latest",
                            },
                        },
                    ),
                ],
                [
                    {
                        "InstanceId": "i-12345",
                        "PrivateIpAddress": "ip-2",
                        "PrivateDnsName": "hostname",
                        "LaunchTime": datetime(2020, 1, 1, tzinfo=timezone.utc),
                    }
                ],
            ),
        ],
        ids=["normal"],
    )
    def test_launch_instances(self, boto3_stubber, launch_params, mocked_boto3_request, expected_assigned_nodes):
        # patch boto3 call
        boto3_stubber("ec2", mocked_boto3_request)
        # run test
        fleet_manager = FleetManagerFactory.get_manager(
            "hit", "region", "boto3_config", FLEET_CONFIG, "queue1", "p4d24xlarge", False, {}, {}
        )
        assigned_nodes = fleet_manager._launch_instances(launch_params)
        assert_that(assigned_nodes.get("Instances", [])).is_equal_to(expected_assigned_nodes)


# -------- Ec2CreateFleetManager ------

test_fleet_spot_params = {
    "LaunchTemplateConfigs": [
        {
            "LaunchTemplateSpecification": {"LaunchTemplateName": "hit-queue1-fleet-spot", "Version": "$Latest"},
            "Overrides": [
                {"MaxPrice": "10", "InstanceType": "t2.medium", "SubnetId": "1234567"},
                {"MaxPrice": "10", "InstanceType": "t2.large", "SubnetId": "1234567"},
            ],
        }
    ],
    "SpotOptions": {
        "AllocationStrategy": "capacity-optimized",
        "SingleInstanceType": False,
        "SingleAvailabilityZone": True,
        "MinTargetCapacity": 1,
    },
    "TargetCapacitySpecification": {"TotalTargetCapacity": 5, "DefaultTargetCapacityType": "spot"},
    "Type": "instant",
}

test_on_demand_params = {
    "LaunchTemplateConfigs": [
        {
            "LaunchTemplateSpecification": {"LaunchTemplateName": "hit-queue2-fleet-ondemand", "Version": "$Latest"},
            "Overrides": [
                {"InstanceType": "t2.medium", "SubnetId": "1234567"},
                {"InstanceType": "t2.large", "SubnetId": "1234567"},
            ],
        }
    ],
    "OnDemandOptions": {
        "AllocationStrategy": "lowest-price",
        "SingleInstanceType": False,
        "SingleAvailabilityZone": True,
        "MinTargetCapacity": 1,
        "CapacityReservationOptions": {"UsageStrategy": "use-capacity-reservations-first"},
    },
    "TargetCapacitySpecification": {"TotalTargetCapacity": 5, "DefaultTargetCapacityType": "on-demand"},
    "Type": "instant",
}


class TestCreateFleetManager:
    @pytest.mark.parametrize(
        (
            "batch_size",
            "queue",
            "compute_resource",
            "all_or_nothing",
            "launch_overrides",
        ),
        [
            # normal - spot
            (
                5,
                "queue1",
                "fleet-spot",
                False,
                {},
            ),
            # normal - on-demand
            (
                5,
                "queue2",
                "fleet-ondemand",
                False,
                {},
            ),
            # all or nothing
            (
                5,
                "queue1",
                "fleet-spot",
                True,
                {},
            ),
            # launch_overrides
            (
                5,
                "queue2",
                "fleet-ondemand",
                False,
                {
                    "queue2": {
                        "fleet-ondemand": {
                            "TagSpecifications": [
                                {"ResourceType": "capacity-reservation", "Tags": [{"Key": "string", "Value": "string"}]}
                            ]
                        }
                    }
                },
            ),
            # Fleet with (Single-Subnet, Multi-InstanceType) AND all_or_nothing is True --> MinTargetCapacity is set
            (
                5,
                "queue4",
                "fleet1",
                True,
                {},
            ),
            # Fleet with (Multi-Subnet, Single-InstanceType) AND all_or_nothing is True --> MinTargetCapacity is set
            (
                5,
                "queue5",
                "fleet1",
                True,
                {},
            ),
            # Fleet with (Multi-Subnet, Multi-InstanceType) AND all_or_nothing is False --> NOT set MinTargetCapacity
            (
                5,
                "queue6",
                "fleet1",
                False,
                {},
            ),
        ],
        ids=[
            "fleet_spot",
            "fleet_ondemand",
            "all_or_nothing",
            "launch_overrides",
            "fleet-single-az-multi-it-all_or_nothing",
            "fleet-multi-az-single-it-all_or_nothing",
            "fleet-multi-az-multi-it",
        ],
    )
    def test_evaluate_launch_params(
        self, batch_size, queue, compute_resource, all_or_nothing, launch_overrides, caplog, test_datadir, request
    ):
        caplog.set_level(logging.INFO)
        # run tests
        fleet_manager = FleetManagerFactory.get_manager(
            "hit", "region", "boto3_config", FLEET_CONFIG, queue, compute_resource, all_or_nothing, {}, launch_overrides
        )
        launch_params = fleet_manager._evaluate_launch_params(batch_size)

        params_path = test_datadir / request.node.callspec.id / "expected_launch_params.json"
        assert_that(launch_params).is_equal_to(json.loads(params_path.read_text()))
        if launch_overrides:
            assert_that(caplog.text).contains("Found CreateFleet parameters override")

    @pytest.mark.parametrize(
        ("launch_params", "mocked_boto3_request", "expected_assigned_nodes"),
        [
            # normal - spot
            (
                test_fleet_spot_params,
                [
                    MockedBoto3Request(
                        method="create_fleet",
                        response={
                            "Instances": [{"InstanceIds": ["i-12345", "i-23456"]}],
                            "Errors": [
                                {"ErrorCode": "InsufficientInstanceCapacity", "ErrorMessage": "Insufficient capacity."}
                            ],
                            "ResponseMetadata": {"RequestId": "1234-abcde"},
                        },
                        expected_params=test_fleet_spot_params,
                    ),
                    MockedBoto3Request(
                        method="describe_instances",
                        response={
                            "Reservations": [
                                {
                                    "Instances": [
                                        {
                                            "InstanceId": "i-12345",
                                            "PrivateIpAddress": "ip-2",
                                            "PrivateDnsName": "hostname",
                                            "LaunchTime": datetime(2020, 1, 1, tzinfo=timezone.utc),
                                        },
                                        {
                                            "InstanceId": "i-23456",
                                            "PrivateIpAddress": "ip-3",
                                            "PrivateDnsName": "hostname",
                                            "LaunchTime": datetime(2020, 1, 1, tzinfo=timezone.utc),
                                        },
                                    ]
                                }
                            ]
                        },
                        expected_params={"InstanceIds": ["i-12345", "i-23456"]},
                        generate_error=False,
                    ),
                ],
                [
                    {
                        "InstanceId": "i-12345",
                        "PrivateIpAddress": "ip-2",
                        "PrivateDnsName": "hostname",
                        "LaunchTime": datetime(2020, 1, 1, tzinfo=timezone.utc),
                    },
                    {
                        "InstanceId": "i-23456",
                        "PrivateIpAddress": "ip-3",
                        "PrivateDnsName": "hostname",
                        "LaunchTime": datetime(2020, 1, 1, tzinfo=timezone.utc),
                    },
                ],
            ),
            # normal - on-demand
            (
                test_on_demand_params,
                [
                    MockedBoto3Request(
                        method="create_fleet",
                        response={
                            "Instances": [{"InstanceIds": ["i-12345"]}],
                            "Errors": [
                                {"ErrorCode": "InsufficientInstanceCapacity", "ErrorMessage": "Insufficient capacity."}
                            ],
                            "ResponseMetadata": {"RequestId": "1234-abcde"},
                        },
                        expected_params=test_on_demand_params,
                    ),
                    MockedBoto3Request(
                        method="describe_instances",
                        response={
                            "Reservations": [
                                {
                                    "Instances": [
                                        {
                                            "InstanceId": "i-12345",
                                            "PrivateIpAddress": "ip-2",
                                            "PrivateDnsName": "hostname",
                                            "LaunchTime": datetime(2020, 1, 1, tzinfo=timezone.utc),
                                        },
                                    ]
                                }
                            ]
                        },
                        expected_params={"InstanceIds": ["i-12345"]},
                        generate_error=False,
                    ),
                ],
                [
                    {
                        "InstanceId": "i-12345",
                        "PrivateIpAddress": "ip-2",
                        "PrivateDnsName": "hostname",
                        "LaunchTime": datetime(2020, 1, 1, tzinfo=timezone.utc),
                    }
                ],
            ),
        ],
        ids=["fleet_spot", "fleet_ondemand"],
    )
    def test_launch_instances(
        self,
        boto3_stubber,
        launch_params,
        mocked_boto3_request,
        expected_assigned_nodes,
        mocker,
    ):
        mocker.patch("time.sleep")
        # patch boto3 call
        boto3_stubber("ec2", mocked_boto3_request)
        # run test
        fleet_manager = FleetManagerFactory.get_manager(
            "hit", "region", "boto3_config", FLEET_CONFIG, "queue2", "fleet-ondemand", False, {}, {}
        )

        assigned_nodes = fleet_manager._launch_instances(launch_params)
        assert_that(assigned_nodes.get("Instances", [])).is_equal_to(expected_assigned_nodes)

    @pytest.mark.parametrize(
        ("instance_ids", "mocked_boto3_request", "expected_exception", "expected_error", "expected_result"),
        [
            # normal - on-demand
            (
                ["i-12345"],
                [
                    MockedBoto3Request(
                        method="describe_instances",
                        response={
                            "Reservations": [
                                {
                                    "Instances": [
                                        {
                                            "InstanceId": "i-12345",
                                            "PrivateIpAddress": "ip-2",
                                            "PrivateDnsName": "hostname",
                                            "LaunchTime": datetime(2020, 1, 1, tzinfo=timezone.utc),
                                        },
                                    ]
                                }
                            ]
                        },
                        expected_params={"InstanceIds": ["i-12345"]},
                        generate_error=False,
                    ),
                ],
                False,
                None,
                (
                    [
                        {
                            "InstanceId": "i-12345",
                            "PrivateIpAddress": "ip-2",
                            "PrivateDnsName": "hostname",
                            "LaunchTime": datetime(2020, 1, 1, tzinfo=timezone.utc),
                        },
                    ],
                    [],
                ),
            ),
            # incomplete instance info
            (
                ["i-12345", "i-23456"],
                [
                    MockedBoto3Request(
                        method="describe_instances",
                        response={
                            "Reservations": [
                                {
                                    "Instances": [
                                        {
                                            # no private dns and address info
                                            "InstanceId": "i-12345",
                                            "LaunchTime": datetime(2020, 1, 1, tzinfo=timezone.utc),
                                        },
                                        {
                                            "InstanceId": "i-23456",
                                            "PrivateIpAddress": "ip-3",
                                            "PrivateDnsName": "hostname",
                                            "LaunchTime": datetime(2020, 1, 1, tzinfo=timezone.utc),
                                        },
                                    ]
                                }
                            ]
                        },
                        expected_params={"InstanceIds": ["i-12345", "i-23456"]},
                        generate_error=False,
                    ),
                    MockedBoto3Request(
                        method="describe_instances",
                        response={
                            "Reservations": [
                                {
                                    "Instances": [
                                        {
                                            "InstanceId": "i-12345",
                                            "PrivateIpAddress": "ip-2",
                                            "PrivateDnsName": "hostname",
                                            "LaunchTime": datetime(2020, 1, 1, tzinfo=timezone.utc),
                                        },
                                    ]
                                }
                            ]
                        },
                        expected_params={"InstanceIds": ["i-12345"]},
                        generate_error=False,
                    ),
                ],
                False,
                None,
                (
                    [
                        {
                            "InstanceId": "i-23456",
                            "PrivateIpAddress": "ip-3",
                            "PrivateDnsName": "hostname",
                            "LaunchTime": datetime(2020, 1, 1, tzinfo=timezone.utc),
                        },
                        {
                            "InstanceId": "i-12345",
                            "PrivateIpAddress": "ip-2",
                            "PrivateDnsName": "hostname",
                            "LaunchTime": datetime(2020, 1, 1, tzinfo=timezone.utc),
                        },
                    ],
                    [],
                ),
            ),
            # too many incomplete instance info
            (
                ["i-12345", "i-23456"],
                [
                    MockedBoto3Request(
                        method="describe_instances",
                        response={
                            "Reservations": [
                                {
                                    "Instances": [
                                        {
                                            # no private dns and address info
                                            "InstanceId": "i-12345",
                                            "LaunchTime": datetime(2020, 1, 1, tzinfo=timezone.utc),
                                        },
                                        {
                                            "InstanceId": "i-23456",
                                            "PrivateIpAddress": "ip-3",
                                            "PrivateDnsName": "hostname",
                                            "LaunchTime": datetime(2020, 1, 1, tzinfo=timezone.utc),
                                        },
                                    ]
                                }
                            ]
                        },
                        expected_params={"InstanceIds": ["i-12345", "i-23456"]},
                        generate_error=False,
                    ),
                ]
                + 2
                * [
                    MockedBoto3Request(
                        method="describe_instances",
                        response={
                            "Reservations": [
                                {
                                    "Instances": [
                                        {
                                            # no private dns and address info
                                            "InstanceId": "i-12345",
                                            "LaunchTime": datetime(2020, 1, 1, tzinfo=timezone.utc),
                                        },
                                    ]
                                }
                            ]
                        },
                        expected_params={"InstanceIds": ["i-12345"]},
                        generate_error=False,
                    ),
                ],
                False,
                "Unable to retrieve instance info for instances: ['i-12345']",
                (
                    [
                        {
                            "InstanceId": "i-23456",
                            "PrivateIpAddress": "ip-3",
                            "PrivateDnsName": "hostname",
                            "LaunchTime": datetime(2020, 1, 1, tzinfo=timezone.utc),
                        },
                    ],
                    ["i-12345"],
                ),
            ),
            # client error
            (
                ["i-12345"],
                [
                    MockedBoto3Request(
                        method="describe_instances",
                        response={},
                        expected_params={"InstanceIds": ["i-12345"]},
                        generate_error=True,
                    ),
                ],
                True,
                "An error occurred .* when calling the DescribeInstances operation",
                ([], []),
            ),
        ],
        ids=["fleet_ondemand", "incomplete_instance_info", "too_many_incomplete_instance_info", "client_error"],
    )
    def test_get_instances_info(  # Note: some tests cases are covered by test_launch_instances too.
        self,
        boto3_stubber,
        mocker,
        instance_ids,
        mocked_boto3_request,
        expected_exception,
        expected_error,
        expected_result,
        caplog,
    ):
        # patch boto3 call
        mocker.patch("time.sleep")
        boto3_stubber("ec2", mocked_boto3_request)
        # run test
        fleet_manager = FleetManagerFactory.get_manager(
            "hit", "region", "boto3_config", FLEET_CONFIG, "queue2", "fleet-ondemand", True, {}, {}
        )

        if expected_exception:
            with pytest.raises(ClientError, match=expected_error):
                fleet_manager._get_instances_info(instance_ids)
        else:
            complete_instances, partial_instance_ids = fleet_manager._get_instances_info(instance_ids)
            assert_that(expected_result).is_equal_to((complete_instances, partial_instance_ids))