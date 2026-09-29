import '../../team/api_client.dart';
import 'profile.dart';

class ProfileRepository {
  ProfileRepository(this._client);

  final ApiClient _client;

  Future<Profile> fetchProfile() async {
    final body = await _client.getJson('/profile');
    if (body case {'name': final String name, 'email': final String email}) {
      return Profile(name: name, email: email);
    }
    throw FormatException('Malformed profile', body);
  }
}
