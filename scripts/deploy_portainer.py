import os
import sys
import json
import requests
import uuid
import re

SUCCESS_STATUS_CODES = {200, 201, 204}


def log_deploy_failure(operation, status_code, response_body, **context):
    print(f"ERROR: {operation} failed", file=sys.stderr)
    for key, value in context.items():
        print(f"  {key}: {value}", file=sys.stderr)
    print(f"  HTTP status: {status_code}", file=sys.stderr)
    print(f"  Response body: {response_body}", file=sys.stderr)


def parse_response_body(response):
    try:
        return response.json()
    except (json.JSONDecodeError, requests.exceptions.JSONDecodeError):
        return response.text


def request_or_exit(method, url, operation, **kwargs):
    try:
        response = requests.request(method, url, verify=False, **kwargs)
    except requests.RequestException as exc:
        print(f"ERROR: {operation} failed — request to Portainer could not be completed", file=sys.stderr)
        print(f"  URL: {url}", file=sys.stderr)
        print(f"  Exception: {type(exc).__name__}: {exc}", file=sys.stderr)
        sys.exit(1)

    if response.status_code not in SUCCESS_STATUS_CODES:
        body = parse_response_body(response)
        log_deploy_failure(operation, response.status_code, body, url=url)
        sys.exit(1)

    return response


def get_environment_id(portainer_url, api_key, environment_name):
    url = f'{portainer_url}/api/endpoints'
    response = request_or_exit(
        'GET',
        url,
        'fetch Portainer endpoints',
        headers={'X-API-Key': api_key},
    )
    env_map = response.json()
    environment = next((env for env in env_map if env['Name'] == environment_name), None)
    if environment is None:
        available = [env.get('Name', '?') for env in env_map]
        print(f"ERROR: Environment '{environment_name}' not found in Portainer.", file=sys.stderr)
        print(f"  Available environments: {available}", file=sys.stderr)
        sys.exit(1)
    return environment['Id']

def get_stacks(portainer_url, api_key, environment_id):
    url = f'{portainer_url}/api/stacks'
    response = request_or_exit(
        'GET',
        url,
        'fetch stacks',
        headers={'X-API-Key': api_key},
        params={'filters': json.dumps({'EndpointId': environment_id})},
    )
    return response.json()

def create_stack(portainer_url, api_key, environment_id, stack_name, compose_file_path, environment_file_path):
    headers = {
        'X-API-Key': f'{api_key}',
        'Content-Type': 'application/json'
    }
    randUuid = uuid.uuid4()


    data={}
    data['stackFileContent'] = open(compose_file_path, 'r').read()
    if(environment_file_path is not None and environment_file_path != ""):
        environmentVars = parse_environment_file(environment_file_path, data['stackFileContent'])
        data['env'] = environmentVars

    data['name'] = stack_name

    url = f'{portainer_url}/api/stacks/create/standalone/string?endpointId={environment_id}'
    print(f"Creating stack '{stack_name}' (endpoint {environment_id})...")
    try:
        response = requests.post(url, headers=headers, json=data, verify=False)
    except requests.RequestException as exc:
        log_deploy_failure(
            'create stack',
            None,
            f'{type(exc).__name__}: {exc}',
            stack=stack_name,
            endpoint_id=environment_id,
            compose_file=compose_file_path,
            url=url,
        )
        sys.exit(1)

    return response.status_code, parse_response_body(response)

def parse_environment_file(environment_file, stack_file_content):
    # Return empty list if environment_file is None
    if environment_file is None:
        return []

    # Read the .env file
    with open(environment_file, 'r') as file:
        environment = file.read()

    # Split lines and filter out empty ones
    environment = environment.split('\n')
    environment = [x for x in environment if x]

    # Split each line into a name-value pair (only on first '=', values may contain '=')
    environment = [x.split('=', 1) for x in environment]

    # Filter out the variables that are used in the stack_file_content
    used_environment = []
    for var in environment:
        if len(var) < 2:
            continue
        name, value = var[0], var[1]
        # Check if the variable name is used in the stack file (e.g., ${NAME})
        if re.search(rf'\${{{name}}}', stack_file_content):
            used_environment.append({"name": name, "value": value.replace("\n", "").replace("\r", "").replace("\t", "").replace("'", "")})

    return used_environment

def update_stack(portainer_url, endpoint_id, api_key, stack_id, file_path, environment_file):
    headers = {
        'X-API-Key': f'{api_key}'
    }

    data = {}

    with open(file_path, 'r') as file:
        compose_file = file.read()
        data['stackFileContent'] = compose_file

    if environment_file is not None and environment_file != "":
        environment = parse_environment_file(environment_file, compose_file)
        data['env'] = environment


    update_url = f'{portainer_url}/api/stacks/{stack_id}?endpointId={endpoint_id}'
    print(f"Updating stack {stack_id} with compose file {file_path}...")
    try:
        response = requests.put(update_url, headers=headers, json=data, verify=False)
    except requests.RequestException as exc:
        log_deploy_failure(
            'update stack',
            None,
            f'{type(exc).__name__}: {exc}',
            stack_id=stack_id,
            endpoint_id=endpoint_id,
            compose_file=file_path,
            url=update_url,
        )
        sys.exit(1)

    return response.status_code, parse_response_body(response)

def main():
    if len(sys.argv) != 5:
        print(f"Expected 4 arguments but got {len(sys.argv) - 1}")
        print("Arguments received:", sys.argv)
        sys.exit(1)

    print("Starting deployment script...")

    portainer_url = sys.argv[1]
    api_key = sys.argv[2]
    changed_files_path = sys.argv[3]
    environment_file = sys.argv[4]

    # Check if environment_file is empty or set to 'default' and handle it properly
    if not environment_file or environment_file == 'default':
        environment_file = None

    if not changed_files_path or not os.path.isfile(changed_files_path):
        print(f"Changed files path is invalid or file not found: {changed_files_path}")
        sys.exit(1)

    # If environment_file is provided and not None, check if it exists
    if environment_file and not os.path.isfile(environment_file):
        print(f"Environment file not found: {environment_file}")
        sys.exit(1)

    with open(changed_files_path, 'r') as file:
        changed_files = file.readlines()

    for file_path in changed_files:
        file_path = file_path.strip()
        if file_path.endswith("docker-compose.yml"):
            parts = file_path.split('/')
            if len(parts) >= 2:
                environment_name = parts[0]
                stack_name = parts[1]
                print(f"Deploying {file_path} → environment '{environment_name}', stack '{stack_name}'")
                environment_id = get_environment_id(portainer_url, api_key, environment_name)

                stacks = get_stacks(portainer_url, api_key, environment_id)
                stack = next((stack for stack in stacks if stack['Name'] == stack_name and stack['EndpointId'] == environment_id), None)
                print(f"Stack '{stack_name}' in environment '{environment_name}': {'found (id ' + str(stack['Id']) + ')' if stack else 'not found, will create'}")

                if stack:
                    status_code, response = update_stack(portainer_url, environment_id, api_key, stack['Id'], file_path, environment_file)
                    if status_code not in SUCCESS_STATUS_CODES:
                        log_deploy_failure(
                            'update stack',
                            status_code,
                            response,
                            stack=stack_name,
                            stack_id=stack['Id'],
                            environment=environment_name,
                            endpoint_id=environment_id,
                            compose_file=file_path,
                            portainer_url=portainer_url,
                        )
                        sys.exit(1)
                    print(f"Updated stack '{stack_name}' in environment '{environment_name}' (HTTP {status_code})")
                else:
                    status_code, response = create_stack(portainer_url, api_key, environment_id, stack_name, file_path, environment_file)
                    if status_code not in SUCCESS_STATUS_CODES:
                        log_deploy_failure(
                            'create stack',
                            status_code,
                            response,
                            stack=stack_name,
                            environment=environment_name,
                            endpoint_id=environment_id,
                            compose_file=file_path,
                            portainer_url=portainer_url,
                        )
                        sys.exit(1)
                    print(f"Created stack '{stack_name}' in environment '{environment_name}' (HTTP {status_code})")

if __name__ == "__main__":
    main()
