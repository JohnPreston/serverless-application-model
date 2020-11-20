import logging
import os
from pathlib import Path
from unittest.case import TestCase

import boto3
import pytest
import yaml
from botocore.exceptions import ClientError
from samcli.lib.deploy.deployer import Deployer
from tests_integ.helpers.helpers import transform_template, verify_stack_resources, generate_suffix, create_bucket
from tests_integ.helpers.file_resources import FILE_TO_S3_URL_MAP, CODE_KEY_TO_FILE_MAP

LOG = logging.getLogger(__name__)
STACK_NAME_PREFIX = "sam-integ-stack-"
S3_BUCKET_PREFIX = "sam-integ-bucket-"


class BaseTest(TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tests_integ_dir = Path(__file__).resolve().parents[1]
        cls.resources_dir = Path(cls.tests_integ_dir, "resources")
        cls.template_dir = Path(cls.resources_dir, "templates", "single")
        cls.output_dir = Path(cls.tests_integ_dir, "tmp")
        cls.expected_dir = Path(cls.resources_dir, "expected", "single")
        cls.code_dir = Path(cls.resources_dir, "code")
        cls.s3_bucket_name = S3_BUCKET_PREFIX + generate_suffix()
        cls.session = boto3.session.Session()
        cls.my_region = cls.session.region_name
        cls.s3_client = boto3.client("s3")
        cls.api_client = boto3.client('apigateway', cls.my_region)
        cls.lambda_client = boto3.client('lambda')
        cls.iam_client = boto3.client('iam')
        cls.api_v2_client = boto3.client('apigatewayv2')
        cls.sfn_client = boto3.client('stepfunctions')

        if not os.path.exists(cls.output_dir):
            os.mkdir(cls.output_dir)

        cls._upload_resources()

    @classmethod
    def tearDownClass(cls):
        cls._clean_bucket()

    @classmethod
    def _clean_bucket(cls):
        """
        Empties and deletes the bucket used for the tests
        """
        s3 = boto3.resource('s3')
        bucket = s3.Bucket(cls.s3_bucket_name)
        object_summary_iterator = bucket.objects.all()

        for object_summary in object_summary_iterator:
            try:
                cls.s3_client.delete_object(Key=object_summary.key, Bucket=cls.s3_bucket_name)
            except ClientError as e:
                LOG.error(
                    "Unable to delete object %s from bucket %s",
                    object_summary.key,
                    cls.s3_bucket_name,
                    exc_info=e
                )
        try:
            cls.s3_client.delete_bucket(Bucket=cls.s3_bucket_name)
        except ClientError as e:
            LOG.error("Unable to delete bucket %s", cls.s3_bucket_name, exc_info=e)

    @classmethod
    def _upload_resources(cls):
        """
        Creates the bucket and uploads the files used by the tests to it
        """
        create_bucket(cls.s3_bucket_name, region=cls.my_region)

        current_file_name = ""

        try:
            for file_name, file_info in FILE_TO_S3_URL_MAP.items():
                current_file_name = file_name
                code_path = str(Path(cls.code_dir, file_name))
                LOG.debug("Uploading file %s to bucket %s", file_name, cls.s3_bucket_name)
                cls.s3_client.upload_file(code_path, cls.s3_bucket_name, file_name)
                LOG.debug("File %s uploaded successfully to bucket %s", file_name, cls.s3_bucket_name)
                file_info["url"] = cls._get_s3_url(file_name, file_info["type"])
        except ClientError as error:
            LOG.error("Upload of file %s to bucket %s failed", current_file_name, cls.s3_bucket_name, exc_info=error)
            cls._clean_bucket()
            raise error

    @classmethod
    def _get_s3_url(cls, file_name, url_type):
        if url_type == "s3":
            return f"s3://{cls.s3_bucket_name}/{file_name}"

        if cls.my_region == "us-east-1":
            return f"https://s3.amazonaws.com/{cls.s3_bucket_name}/{file_name}"
        if cls.my_region == "us-iso-east-1":
            return f"https://s3.us-iso-east-1.c2s.ic.gov/{cls.s3_bucket_name}/{file_name}"
        if cls.my_region == "us-isob-east-1":
            return f"https://s3.us-isob-east-1.sc2s.sgov.gov/{cls.s3_bucket_name}/{file_name}"

        return f"https://s3-{cls.my_region}.amazonaws.com/{cls.s3_bucket_name}/{file_name}"

    def setUp(self):
        self.cloudformation_client = boto3.client("cloudformation")
        self.deployer = Deployer(self.cloudformation_client, changeset_prefix="sam-integ-")

    def tearDown(self):
        self.cloudformation_client.delete_stack(StackName=self.stack_name)
        if os.path.exists(self.output_file_path):
            os.remove(self.output_file_path)
        if os.path.exists(self.sub_input_file_path):
            os.remove(self.sub_input_file_path)

    def create_and_verify_stack(self, file_name, parameters=[]):
        """
        Creates the Cloud Formation stack and verifies it against the expected
        result

        Parameters
        ----------
        file_name : string
            Template file name
        parameters : list
            List of parameters
        """
        self.output_file_path = str(Path(self.output_dir, "cfn_" + file_name + ".yaml"))
        self.expected_resource_path = str(Path(self.expected_dir, file_name + ".json"))
        self.stack_name = STACK_NAME_PREFIX + file_name.replace("_", "-") + "-" + generate_suffix()

        self._fill_template(file_name)
        self.transform_template()
        self.deploy_stack(parameters)
        self.verify_stack()

    def transform_template(self):
        transform_template(self.sub_input_file_path, self.output_file_path)
    
    def get_region(self):
        return self.my_region

    def get_s3_uri(self, file_name):
        """
        Returns the S3 URI of a resource file

        Parameters
        ----------
        file_name : string
            Resource file name
        """
        return FILE_TO_S3_URL_MAP[file_name]["url"]

    def get_code_key_s3_uri(self, code_key):
        """
        Returns the S3 URI of a code key for template replacement

        Parameters
        ----------
        code_key : string
            Template code key
        """
        return FILE_TO_S3_URL_MAP[CODE_KEY_TO_FILE_MAP[code_key]]["url"]

    def get_stack_resources(self, resource_type, stack_resources=None):
        if not stack_resources:
            stack_resources = self.stack_resources

        resources = []
        for res in stack_resources["StackResourceSummaries"]:
            if res["ResourceType"] == resource_type:
                resources.append(res)

        return resources
    
    def get_stack_output(self, output_key):
        for output in self.stack_description["Stacks"][0]["Outputs"]:
            if output["OutputKey"] == output_key:
                return output
        return None

    def get_stack_tags(self, output_name):
        resource_arn = self.get_stack_output(output_name)["OutputValue"]
        return self.sfn_client.list_tags_for_resource(resourceArn=resource_arn)["tags"]

    def get_stack_deployment_ids(self):
        resources = self.get_stack_resources("AWS::ApiGateway::Deployment")
        ids = []
        for res in resources:
            ids.append(res["LogicalResourceId"])

        return ids

    def get_stack_stages(self):
        resources = self.get_stack_resources("AWS::ApiGateway::RestApi")

        if not resources:
            return []

        return self.api_client.get_stages(restApiId=resources[0]["PhysicalResourceId"])["item"]

    def get_stack_stages_v2(self):
        resources = self.get_stack_resources("AWS::ApiGatewayV2::Api")

        if not resources:
            return None

        return self.api_v2_client.get_stages(ApiId=resources[0]["PhysicalResourceId"])["Items"]

    def get_stack_nested_stack_resources(self):
        resources = self.get_stack_resources("AWS::CloudFormation::Stack")

        if not resources:
            return None

        return self.cloudformation_client.list_stack_resources(StackName=resources[0]["PhysicalResourceId"])

    def get_stack_outputs(self):
        if not self.stack_description:
            return {}
        else:
            output_key_to_value = {}
            output_list = self.stack_description["Stacks"][0]["Outputs"]
            for output in output_list:
                output_key_to_value[output["OutputKey"]] = output["OutputValue"]
            return output_key_to_value

    def get_resource_status_by_logical_id(self, logical_id):
        if not self.stack_resources:
            return ""

        for res in self.stack_resources["StackResourceSummaries"]:
            if res["LogicalResourceId"] == logical_id:
                return res["ResourceStatus"]

        return ""

    def get_physical_id_by_type(self, resource_type):
        if not self.stack_resources:
            return ""

        for res in self.stack_resources["StackResourceSummaries"]:
            if res["ResourceType"] == resource_type:
                return res["PhysicalResourceId"]

        return ""

    def get_logical_id_by_type(self, resource_type):
        if not self.stack_resources:
            return ""

        for res in self.stack_resources["StackResourceSummaries"]:
            if res["ResourceType"] == resource_type:
                return res["LogicalResourceId"]

        return ""

    def get_physical_id_by_logical_id(self, logical_id):
        if not self.stack_resources:
            return ""

        for res in self.stack_resources["StackResourceSummaries"]:
            if res["LogicalResourceId"] == logical_id:
                return res["PhysicalResourceId"]

        return ""

    def _fill_template(self, file_name):
        """
        Replaces the template variables with their value

        Parameters
        ----------
        file_name : string
            Template file name
        """
        input_file_path = str(Path(self.template_dir, file_name + ".yaml"))
        updated_template_path = str(Path(self.output_dir, "sub_" + file_name + ".yaml"))
        with open(input_file_path, "r") as f:
            data = f.read()
        for key, _ in CODE_KEY_TO_FILE_MAP.items():
            data = data.replace(f"${{{key}}}", self.get_code_key_s3_uri(key))
        yaml_doc = yaml.load(data, Loader=yaml.FullLoader)

        with open(updated_template_path, "w") as f:
            yaml.dump(yaml_doc, f)

        self.sub_input_file_path = updated_template_path

    def set_template_resource_property(self, resource_name, property_name, value):
        """
        Updates a resource property of the current SAM template

        Parameters
        ----------
        resource_name: string
            resource name
        property_name: string
            property name
        value
            value
        """
        with open(self.sub_input_file_path, "r") as f:
            data = f.read()
        yaml_doc = yaml.load(data, Loader=yaml.FullLoader)
        yaml_doc['Resources'][resource_name]["Properties"][property_name] = value

        with open(self.sub_input_file_path, "w") as f:
            yaml.dump(yaml_doc, f)

    def get_template_resource_property(self, resource_name, property_name):
        with open(self.sub_input_file_path, "r") as f:
            data = f.read()
        yaml_doc = yaml.load(data, Loader=yaml.FullLoader)

        return yaml_doc['Resources'][resource_name]["Properties"][property_name]

    def deploy_stack(self, parameters=[]):
        """
        Deploys the current cloud formation stack
        """
        with open(self.output_file_path, "r") as cfn_file:
            result, changeset_type = self.deployer.create_and_wait_for_changeset(
                stack_name=self.stack_name,
                cfn_template=cfn_file.read(),
                parameter_values=parameters,
                capabilities=["CAPABILITY_IAM", "CAPABILITY_AUTO_EXPAND"],
                role_arn=None,
                notification_arns=[],
                s3_uploader=None,
                tags=[],
            )
            self.deployer.execute_changeset(result["Id"], self.stack_name)
            self.deployer.wait_for_execute(self.stack_name, changeset_type)

        self.stack_description = self.cloudformation_client.describe_stacks(StackName=self.stack_name)
        self.stack_resources = self.cloudformation_client.list_stack_resources(StackName=self.stack_name)

    def verify_stack(self):
        """
        Gets and compares the Cloud Formation stack against the expect result file
        """
        # verify if the stack was successfully created
        self.assertEqual(self.stack_description["Stacks"][0]["StackStatus"], "CREATE_COMPLETE")
        # verify if the stack contains the expected resources
        self.assertTrue(verify_stack_resources(self.expected_resource_path, self.stack_resources))
