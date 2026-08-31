class AgentFlow < Formula
  include Language::Python::Virtualenv

  desc "CLI workflow kit that runs AI coding agents on a verifiable process"
  homepage "https://github.com/chonamdoo/agent-flow"
  url "https://github.com/chonamdoo/agent-flow/archive/refs/tags/v0.2.8.tar.gz"
  sha256 "997662a09f71c49a13ec497ec994962d5a61281b114ed740b2214aa7b97db7a8"
  license "MIT"
  head "https://github.com/chonamdoo/agent-flow.git", branch: "main"

  # libyaml is what the pyyaml resource compiles its fast loader against;
  # brew audit requires it to be declared alongside that resource.
  depends_on "libyaml"
  # node is a runtime dependency, not a build one: project install
  # (`agent-flow <path>`) and every managed hook run bin/agent-flow-kit.mjs.
  depends_on "node"
  depends_on "python@3.13"

  resource "pyyaml" do
    url "https://files.pythonhosted.org/packages/05/8e/961c0007c59b8dd7729d542c61a4d537767a59645b82a0b521206e1e25c2/pyyaml-6.0.3.tar.gz"
    sha256 "d76623373421df22fb4cf8817020cbb7ef15c725b9d5e45f17e189bfc384190f"
  end

  resource "click" do
    url "https://files.pythonhosted.org/packages/76/d4/81420972a676e8ffea40450d8c8c92943e7218a78fe9b64359836cc9876b/click-8.4.2.tar.gz"
    sha256 "9a6cea6e60b17ebe0a44c5cc636d94f09bd66142c1cd7d8b4cd731c4917a15f6"
  end

  def install
    # The kit root is the directory that holds both pyproject.toml and
    # bin/agent-flow-kit.mjs (src/agent_flow/core/phase_workflow.py,
    # find_kit_root). Keeping the asset tree in libexec and the virtualenv
    # under it makes libexec an ancestor of the installed package, so the
    # workflow, profile, and skill sources resolve without any env var.
    # README.md is a build input, not documentation here: pyproject.toml declares
    # `readme = "README.md"`, so hatchling aborts metadata generation without it.
    libexec.install "bin", "lib", "scripts", "skills", "templates", "bootstrap",
                    "src", ".Codex", ".claude", "package.json", "pyproject.toml",
                    "README.md"
    venv = virtualenv_create(libexec/"venv", "python3.13")
    venv.pip_install resources
    venv.pip_install libexec
    # A wrapper instead of the plain symlink: project install and the hooks
    # spawn node, and PATH is not guaranteed wherever the host CLI launches
    # them from (launchd, CI, another process). The caller's PATH is kept because
    # the reviewers, git, and gh all come from it; the fallback exists so an unset
    # PATH cannot leave an empty entry, which resolves relative to the cwd.
    (bin/"agent-flow").write_env_script libexec/"venv/bin/agent-flow",
                                        PATH: "#{formula_opt_bin("node")}:" \
                                              "${PATH:-#{HOMEBREW_PREFIX}/bin:/usr/bin:/bin}"
  end

  test do
    assert_match(/^agent-flow \d+\.\d+/, shell_output("#{bin}/agent-flow --version"))
    # The asset tree is the half that a linked executable alone does not prove.
    assert_match "\"id\": \"bugfix\"",
                 shell_output("#{bin}/agent-flow workflow export --workflow bugfix")
    # `agent-flow <dir>` runs the Node installer, so this covers the wrapper's
    # PATH as well as the packaged assets.
    (testpath/"project").mkpath
    system bin/"agent-flow", testpath/"project"
    assert_path_exists testpath/"project/.agent-flow/kit.json"
  end
end
