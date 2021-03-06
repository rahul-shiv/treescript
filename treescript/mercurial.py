"""Treescript mercurial functions."""
import logging
import os
import sys

from treescript.utils import execute_subprocess, DONTBUILD_MSG
from treescript.exceptions import FailedSubprocess
from treescript.task import get_source_repo, get_tag_info, get_dontbuild

# https://www.mercurial-scm.org/repo/hg/file/tip/tests/run-tests.py#l1040
# For environment vars.

HGRCPATH = os.path.join(os.path.dirname(__file__), 'data', 'hgrc')
ROBUSTCHECKOUT_PATH = os.path.abspath(
    os.path.join(os.path.dirname(__file__), 'py2', 'robustcheckout.py')
)
TAG_MSG = "No bug - Tagging {revision} with {tags} a=release CLOSED TREE"

log = logging.getLogger(__name__)


# build_hg_command {{{1
def build_hg_command(context, *args):
    """Generate a mercurial command to run.

    See-Also ``build_hg_environment``

    Args:
        context (TreeScriptContext): the treescript context
        *args: the remaining args to pass to the hg command

    Returns:
        list: the hg command to run.

    """
    hg = context.config['hg']
    if not isinstance(hg, (list, tuple)):
        hg = [hg]
    robustcheckout_args = [
        '--config', 'extensions.robustcheckout={}'.format(ROBUSTCHECKOUT_PATH)
    ]
    return hg + [*robustcheckout_args, *args]


# build_hg_environment {{{1
def build_hg_environment(context):
    """Generate an environment suitable for running mercurial programatically.

    This function sets the hgrc to one provided in the package and ensures
    environment variables which affect HG are defined in a stable way.

    See-Also ``build_hg_command``, ``run_hg_command``

    Returns:
        list: the environment to use.

    """
    env = os.environ.copy()
    if context.config['hgrc']:
        env['HGRCPATH'] = context.config['hgrc']
        if not os.path.exists(env['HGRCPATH']):
            log.warning(FileNotFoundError(env['HGRCPATH']))
            log.info('Using default hgrc')
            env['HGRCPATH'] = HGRCPATH
    else:
        env['HGRCPATH'] = HGRCPATH
    env['HGEDITOR'] = ('"' + sys.executable + '"' + ' -c "import sys; sys.exit(0)"')
    env["HGMERGE"] = "internal:merge"
    env["HGENCODING"] = "utf-8"
    env['HGPLAIN'] = '1'
    env['LANG'] = env['LC_ALL'] = env['LANGUAGE'] = 'C'
    env['TZ'] = 'GMT'
    # List found at
    # https://www.mercurial-scm.org/repo/hg/file/ab239e3de23b/tests/run-tests.py#l1076
    for k in ('HG HGPROF CDPATH GREP_OPTIONS http_proxy no_proxy ' +
              'HGPLAINEXCEPT EDITOR VISUAL PAGER NO_PROXY CHGDEBUG').split():
        if k in env:
            del env[k]
    return env


# run_hg_command {{{1
async def run_hg_command(context, *args, local_repo=None):
    """Run a mercurial command.

    See-Also ``build_hg_environment``, ``build_hg_command``

    Args:
        context (TreeScriptContext): the treescript context
        *str: the remaining args to pass to the hg command

    Returns:
        list: the hg command to run.

    """
    command = build_hg_command(context, *args)
    env = build_hg_environment(context)
    if local_repo:
        command.extend(['-R', local_repo])
    await execute_subprocess(command, env=env)


# log_mercurial_version {{{1
async def log_mercurial_version(context):
    """Run mercurial '-v version' to get used version into logs.

    Args:
        context (TreeScriptContext): the treescript context

    """
    await run_hg_command(context, '-v', 'version')


# validate_robustcheckout_works {{{1
async def validate_robustcheckout_works(context):
    """Validate that the robustcheckout extension works.

    This works by trying to run 'hg robustcheckout -q --help' on
    hg as defined by our context object.

    Args:
        context (TreeScriptContext): the treescript context

    Returns:
        bool: True if robustcheckout seems to work, False otherwise.

    """
    try:
        await run_hg_command(context, 'robustcheckout', '-q', '--help')
        return True
    except FailedSubprocess:
        return False


# checkout_repo {{{1
async def checkout_repo(context, directory):
    """Perform a clone via robustcheckout, at ${directory}/src.

    This function will perform a clone via robustcheckout, using hg's share extension
    for a cache at 'context.config['hg_share_base_dir']' that robustcheckout will
    populate if necessary.

    Robustcheckout will retry network operations at most 3 times (robustcheckout's default)
    before giving up and causing FailedSubprocess to be raised.

    Args:
        context (TreeScriptContext): the treescript context
        directory (str): The directory to place the resulting clone.

    Raises:
        FailedSubprocess: if the clone attempt doesn't succeed.

    """
    share_base = context.config['hg_share_base_dir']
    upstream_repo = context.config['upstream_repo']
    dest_repo = get_source_repo(context.task)
    dest_folder = os.path.join(directory, 'src')
    context.repo = dest_folder
    # branch default is used to pull tip of the repo at checkout time
    await run_hg_command(context, 'robustcheckout', dest_repo, dest_folder,
                         '--sharebase', share_base,
                         '--upstream', upstream_repo,
                         '--branch', 'default')


# do_tagging {{{1
async def do_tagging(context, directory):
    """Perform tagging, at ${directory}/src.

    This function will perform a mercurial tag, on 'default' head of target repository.
    It will tag the revision specified in the tag_info portion of the task payload, using
    the specified tags in that payload.

    Tags are forced to be created at the specified revision if they already existed.

    This function has the side affect of pulling the specified revision from the
    destination repository. This feature exists because mozilla-unified does not
    contain relbranches, though some releases are created on relbranches, so we must ensure
    the desired revision to tag is known to the local repository.

    Args:
        context (TreeScriptContext): the treescript context
        directory (str): The directory to place the resulting clone.

    Raises:
        FailedSubprocess: if the tag attempt doesn't succeed.

    """
    local_repo = os.path.join(directory, 'src')
    tag_info = get_tag_info(context.task)
    desired_tags = tag_info['tags']
    desired_rev = tag_info['revision']
    dontbuild = get_dontbuild(context.task)
    dest_repo = get_source_repo(context.task)
    commit_msg = TAG_MSG.format(revision=desired_rev, tags=', '.join(desired_tags))
    if dontbuild:
        commit_msg += DONTBUILD_MSG
    log.info("Pulling {revision} from {repo} explicitly.".format(
        revision=desired_rev, repo=dest_repo))
    await run_hg_command(context, 'pull', '-r', desired_rev, dest_repo,
                         local_repo=local_repo)
    log.info(commit_msg)
    await run_hg_command(context, 'tag', '-m', commit_msg, '-r', desired_rev,
                         '-f',  # Todo only force if needed
                         *desired_tags,
                         local_repo=local_repo)


async def log_outgoing(context, directory):
    """Run `hg out` against the current revision in the repository.

    This logs current changes that will be pushed (or would have been, if dry-run)
    """
    local_repo = os.path.join(directory, 'src')
    dest_repo = get_source_repo(context.task)
    log.info("outgoing changesets..")
    await run_hg_command(context, 'out', '-vp', '-r', '.', dest_repo, local_repo=local_repo)


async def push(context):
    """Run `hg push` against the current source repo."""
    local_repo = context.repo
    dest_repo = get_source_repo(context.task)
    dest_repo_ssh = dest_repo.replace('https://', 'ssh://')
    ssh_username = context.config.get("hg_ssh_user")
    ssh_key = context.config.get("hg_ssh_keyfile")
    ssh_opt = []
    if ssh_username or ssh_key:
        ssh_opt = ['-e', 'ssh']
        if ssh_username:
            ssh_opt[1] += ' -l %s' % ssh_username
        if ssh_key:
            ssh_opt[1] += ' -i %s' % ssh_key
    log.info("Pushing local changes to {}".format(dest_repo_ssh))
    await run_hg_command(context, 'push', *ssh_opt, '-r', '.', '-v',
                         dest_repo_ssh, local_repo=local_repo)
