/// Team-wide error sink. The production build forwards these to the crash reporter.
final List<String> teamErrorLog = <String>[];

void reportTeamError(String feature, Object error) {
  teamErrorLog.add('$feature: $error');
}
