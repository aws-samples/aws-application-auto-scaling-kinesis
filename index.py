'''
Copyright 2018 Amazon.com, Inc. or its affiliates. All Rights Reserved.

Permission is hereby granted, free of charge, to any person obtaining a copy of this
software and associated documentation files (the "Software"), to deal in the Software
without restriction, including without limitation the rights to use, copy, modify,
merge, publish, distribute, sublicense, and/or sell copies of the Software, and to
permit persons to whom the Software is furnished to do so.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED,
INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A
PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT
HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION
OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE
SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.
'''

import boto3
import json
import os

client_kinesis = boto3.client('kinesis')
client_ssm = boto3.client('ssm')
client_cloudwatch = boto3.client('cloudwatch')
client_lambda = boto3.client('lambda')
client_cloudformation = boto3.client('cloudformation')

PARAMETER_STORE = os.environ['ParameterStore']
AUTOSCALINGPOLICYOUT_ARN = ''
AUTOSCALINGPOLICYIN_ARN = ''
CLOUDWATCHALARMNAMEOUT = os.environ['CloudWatchAlarmNameOut']
CLOUDWATCHALARMNAMEIN = os.environ['CloudWatchAlarmNameIn']

def update_shards(desiredCapacity, resourceName):

	# Update the shard count to the new Desired Capacity value
	try: 
		response = client_kinesis.update_shard_count(
			StreamName=resourceName,
			TargetShardCount=int(desiredCapacity),
			ScalingType='UNIFORM_SCALING'
		) 
		print("Response: ", response)
		scalingStatus = "InProgress"

		#need also to update alarm threshold using the put_metric_alarm
		update_alarm_out(desiredCapacity, resourceName)
		update_alarm_in(desiredCapacity, resourceName)

	# In case of error of updating the sharding, raise an exception. Possible cause, you cannot reshard more than twice a day
	except Exception as e:
		print (e)
		failureReason = str(e)
		scalingStatus = "Failed"
		pass
	
	return scalingStatus		


#fuction to update scale out alarm threshol
def update_alarm_out(shards, stream):
	new_threshold = (1000 * shards * 60)*80/100 #assuming alarm will fire at 80% of incoming records
	try: 
		set_alarm = client_cloudwatch.put_metric_alarm(
			AlarmName=CLOUDWATCHALARMNAMEOUT,
			AlarmDescription='incomingRecord exceeds threshold',
			MetricName='IncomingRecords',
			Namespace='AWS/Kinesis',
			Dimensions=[
				{
					'Name':'StreamName',
					'Value':stream
				}
				],
			Statistic='Sum',
			Period=60,
			EvaluationPeriods=1,
			Threshold=new_threshold,
			ComparisonOperator='GreaterThanThreshold',
			AlarmActions=[
				AUTOSCALINGPOLICYOUT_ARN
				]
			)
	except Exception as e:
		print (e)

#fuction to update scale in alarm threshol
def update_alarm_in(shards, stream):
	new_threshold = (1000 * shards * 60)*80/100 #assuming alarm will fire at 80% of incoming records
	try:
		set_alarm = client_cloudwatch.put_metric_alarm(
			AlarmName=CLOUDWATCHALARMNAMEIN,
			AlarmDescription='incomingRecord below threshold',
			MetricName='IncomingRecords',
			Namespace='AWS/Kinesis',
			Dimensions=[
				{
					'Name':'StreamName',
					'Value':stream
				}
				],
			Statistic='Sum',
			Period=300,
			EvaluationPeriods=3,
			Threshold=new_threshold,
			ComparisonOperator='LessThanThreshold',
			AlarmActions=[
				AUTOSCALINGPOLICYIN_ARN
				]
			)
	except Exception as e:
		print (e)

def response_function(status_code, response_body):
	return_json = {
				'statusCode': status_code,
				'body': json.dumps(response_body) if response_body else json.dumps({}),
				'headers': {
					'Content-Type': 'application/json',
				},
			}
	# log response
	print (return_json)
	return return_json

#trick for updating environment variable with application autoscaling arn (need to update all the current variables)
def autoscaling_policy_arn(context):
	print (context.function_name)
	function_name = context.function_name
	stack_name = context.function_name.split('LambdaScaler')[0][:-1]
	print (stack_name)
	
	response = client_cloudformation.describe_stack_resources(
	StackName=stack_name,
	LogicalResourceId='AutoScalingPolicyOut'
	)

	AutoScalingPolicyOut = response['StackResources'][0]['PhysicalResourceId']
	print ('Autoscaling Policy Out: ' +AutoScalingPolicyOut)
	response2 = client_cloudformation.describe_stack_resources(
	StackName=stack_name,
	LogicalResourceId='AutoScalingPolicyIn'
	)
	
	AutoScalingPolicyIn = response2['StackResources'][0]['PhysicalResourceId']
	print ('Autoscaling Policy In: ' +AutoScalingPolicyIn)
	
	response = client_lambda.update_function_configuration(
		FunctionName=function_name,
		Timeout=3,
		Environment={
			'Variables' : {
				'AutoScalingPolicyOut': AutoScalingPolicyOut,
				'AutoScalingPolicyIn': AutoScalingPolicyIn,
				'ParameterStore': PARAMETER_STORE,
				'CloudWatchAlarmNameOut': CLOUDWATCHALARMNAMEOUT,
				'CloudWatchAlarmNameIn': CLOUDWATCHALARMNAMEIN
			}
		}
	)  
	print (response)
	return


def lambda_handler(event, context):

	# log the event
	print (json.dumps(event))


	# get Stream name
	if 'scalableTargetDimensionId' in event['pathParameters']:
		resourceName = event['pathParameters']['scalableTargetDimensionId']
		print (resourceName)
	else:
		message = "Error, scalableTargetDimensionId not found"
		return response_function(400, str(message))


	# try to get information of the Kinesis stream
	try: 
		response = client_kinesis.describe_stream_summary(
			StreamName=resourceName,
		)
		print(response)
		streamStatus = response['StreamDescriptionSummary']['StreamStatus']
		shardsNumber = response['StreamDescriptionSummary']['OpenShardCount']
		actualCapacity = shardsNumber
	except Exception as e:
		message = "Error, cannot find a Kinesis stream called " + resourceName
		return response_function(404, message)


	# try to retrive the desired capacity from ParameterStore
	
	response = client_ssm.get_parameter(
		Name=PARAMETER_STORE
		)
	print(response)
	
	if 'Parameter' in response:
		if 'Value' in response['Parameter']:
			desiredCapacity = response['Parameter']['Value']
			print(desiredCapacity)
	else:
		# if I do not have an entry in ParameterStore, I assume that the desiredCapacity is like the actualCapacity 
		desiredCapacity = actualCapacity


	if streamStatus == "UPDATING":
		scalingStatus = "InProgress"
	elif streamStatus == "ACTIVE":
		scalingStatus = "Successful"


	if event['httpMethod'] == "PATCH":

		# Check whether autoscaling is calling to change the Desired Capacity
		if 'desiredCapacity' in event['body']:
			desiredCapacityBody = json.loads(event['body'])
			desiredCapacityBody = desiredCapacityBody['desiredCapacity']

			# Check whether the new desired capacity is negative. If so, I need to calculate the new desired capacity
			if int(desiredCapacityBody) >= 0:
				desiredCapacity = desiredCapacityBody

				# Store the new desired capacity in a ParamenterStore
				response = client_ssm.put_parameter(
				    Name=PARAMETER_STORE,
				    Value=str(int(desiredCapacity)),
				    Type='String',
				    Overwrite=True
				)
				print(response)
				print ("Trying to set capacity to "+ str(desiredCapacity))
				
				global AUTOSCALINGPOLICYOUT_ARN
				global AUTOSCALINGPOLICYIN_ARN
				if 'AutoScalingPolicyOut' and 'AutoScalingPolicyIn' not in os.environ:
					autoscaling_policy_arn(context)
				AUTOSCALINGPOLICYOUT_ARN = os.environ['AutoScalingPolicyOut']
				AUTOSCALINGPOLICYIN_ARN = os.environ['AutoScalingPolicyIn']

				scalingStatus = update_shards(desiredCapacity,resourceName)

	if scalingStatus == "Successful" and float(desiredCapacity) != float(actualCapacity):		
		scalingStatus = update_shards(desiredCapacity,resourceName)

		
	returningJson = {
	  "actualCapacity": float(actualCapacity),
	  "desiredCapacity": float(desiredCapacity),
	  "dimensionName": resourceName,
	  "resourceName": resourceName,
	  "scalableTargetDimensionId": resourceName,
	  "scalingStatus": scalingStatus,
	  "version": "MyVersion"
	}
	
	try:
		returningJson['failureReason'] = failureReason
	except:
		pass
	
	print(returningJson)

	return response_function(200, returningJson)