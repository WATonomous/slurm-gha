GITHUB_API_BASE_URL = 'https://api.github.com/repos/WATonomous/infra-config' 
GITHUB_REPO_URL = 'https://github.com/WATonomous/infra-config'
ALLOCATE_RUNNER_SCRIPT_PATH = "apptainer.sh" # relative path from '/allocation_script' 


REPOS_TO_MONITOR = [
    {
        'name': 'WATonomous/infra-config',
        'api_base_url': 'https://api.github.com/repos/WATonomous/infra-config',
        'repo_url': 'https://github.com/WATonomous/infra-config'
    },
	{
		'name': 'WATonomous/wato_asd_training',
		'api_base_url': 'https://api.github.com/repos/WATonomous/wato_asd_training',
		'repo_url': 'https://github.com/WATonomous/wato_asd_training'
	}
    
]